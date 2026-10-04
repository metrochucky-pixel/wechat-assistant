"""Visit-scoped frame reuse and persistent timing; never caches send authorization."""
import contextvars
import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import date
from functools import wraps

_current = contextvars.ContextVar('bot_visit', default=None)


def frame_key(image):
    area = image.crop((0, int(image.height*.045), image.width, image.height)).convert('RGB')
    return hashlib.sha256(str((image.size,date.today().isoformat())).encode()+area.tobytes()).hexdigest()


def note(kind, value):
    trace = _current.get()
    if trace is not None:
        trace['events'].append({'kind':kind,'value':value})


def cached(image, namespace, value=None):
    trace = _current.get()
    if trace is None or os.environ.get('WECHATBOT_CACHE_VISUAL','1') != '1':
        return None
    key = (namespace,frame_key(image))
    if value is not None:
        if len(trace['frames'])>=24:
            trace['frames'].pop(next(iter(trace['frames'])))
        trace['frames'][key]=(time.monotonic(),copy.deepcopy(value))
        return value
    entry = trace['frames'].get(key)
    if entry and time.monotonic()-entry[0] <= 30:
        trace['cache_hits']+=1
        note('cache_hit',namespace)
        return copy.deepcopy(entry[1])
    return None


@contextmanager
def stage(name):
    start=time.monotonic(); failed=False
    try:
        yield
    except BaseException:
        failed=True
        raise
    finally:
        trace=_current.get()
        if trace is not None:
            trace['stages'].append({'name':name,'ms':round((time.monotonic()-start)*1000,1),'failed':failed})


def timed(name):
    def wrap(fn):
        @wraps(fn)
        def run(*a,**kw):
            with stage(name):return fn(*a,**kw)
        return run
    return wrap


def outcome(action):
    if action.startswith('dry_run_'):return 'dry_run'
    if '后台' in action:return 'background_pending'
    if action in ('已发送','已发图'):return 'sent'
    if '未确认' in action and '发送' in action:return 'send_uncertain'
    if '会话身份' in action:return 'identity_unverified'
    if '仅被@' in action:return 'mention_required'
    if '观察不完整' in action or '需刷新' in action or '观察' in action:return 'observation_incomplete'
    if '草稿' in action:return 'draft_blocked'
    if '熔断' in action or '退避' in action:return 'backoff'
    if '确认' in action:return 'owner_required'
    if '无需回复' in action:return 'no_reply_needed'
    if '技术问题' in action:return 'technical_skipped'
    if '自动重读' in action:return 'technical_retry'
    if '失败' in action:return 'failed'
    return 'deferred_or_skipped'


def visit(outbox):
    def wrap(fn):
        @wraps(fn)
        def run(*a,**kw):
            # Nested calls share one phone visit; worker threads have their own context.
            if _current.get() is not None:return fn(*a,**kw)
            trace={'frames':{},'cache_hits':0,'stages':[],'events':[]}
            token=_current.set(trace);start=time.monotonic();result=None;error=''
            conv=kw.get('conv_hint') or (a[1] if len(a)>1 else '')
            try:
                result=fn(*a,**kw)
                return result
            except BaseException as exc:
                error=type(exc).__name__
                raise
            finally:
                action=result.get('_action','') if isinstance(result,dict) else ''
                report={k:trace[k] for k in ('cache_hits','stages','events')}
                report.update(total_ms=round((time.monotonic()-start)*1000,1),action=action,
                              outcome='exception' if error else outcome(action),error=error)
                report['api_calls']=sum(e['kind']=='api_call' for e in trace['events'])
                try:
                    with outbox().connect() as db:
                        db.execute('CREATE TABLE IF NOT EXISTS performance_visits(id INTEGER PRIMARY KEY, observed REAL, display TEXT, outcome TEXT, total_ms REAL, payload TEXT)')
                        db.execute('INSERT INTO performance_visits(observed,display,outcome,total_ms,payload) VALUES(?,?,?,?,?)',
                                   (time.time(),conv,report['outcome'],report['total_ms'],json.dumps(report,ensure_ascii=False)))
                        db.execute('DELETE FROM performance_visits WHERE id <= (SELECT MAX(id)-2000 FROM performance_visits)')
                    print('[performance] %s %.2fs api=%d cache=%d %s' % (conv,report['total_ms']/1000,report['api_calls'],report['cache_hits'],report['outcome']))
                except Exception as exc:
                    print('[performance] write failed '+type(exc).__name__)
                finally:_current.reset(token)
        return run
    return wrap


def text_only(snapshot):
    messages=snapshot.get('messages')
    if not isinstance(messages,list) or not messages:return False
    if any(not isinstance(m,dict) or m.get('certain') is not True or not m.get('sender')
           or not isinstance(m.get('text'),str) for m in messages):return False
    text='\n'.join(m['text']+' '+str(m.get('quote','')) for m in messages)
    return not any(mark in text for mark in ('[图片]','[视频]','[语音]','[文件]','[表情包]','[链接]','[卡片]'))


def simple_eligible(payload, snapshot):
    """Conservative routing only. The draft model may still escalate to complex/owner."""
    if payload.get('_request_kind')!='chat':return False
    if snapshot.get('is_chat') is not True or snapshot.get('at_latest') is not True:return False
    if any(payload.get(k) for k in ('needs_image','wants_image','offer_image','is_deep_q','_needs_owner','needs_human','_owner_reconfirm','_owner_approved')):return False
    if not text_only(snapshot):return False
    messages=snapshot['messages']
    text='\n'.join(m['text']+' '+str(m.get('quote','')) for m in messages)
    target=str(payload.get('question_to_answer',''))+' '+str(payload.get('question_context',''))
    if len(text)>2400 or len(target)>1000:return False
    # Full structured context is retained. Images and personal commitments go to the existing path.
    blockers=('[图片]','[视频]','[语音]','[文件]','http','多少钱','报价','转账','付款','红包','借钱','答应','承诺','约见','查一下','查资料','最新','你认识','你去过','你喝过','我去过','我喝过')
    return not any(x in text+target for x in blockers)
