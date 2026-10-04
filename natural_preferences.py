"""Source-bound natural feedback candidates. Fixed style outputs, never free-form rules."""
import contextvars
import hashlib
import json
import re
import time
from contextlib import contextmanager
from functools import wraps
from bot_reliability import conversation_key

_current=contextvars.ContextVar('owner_style_conversation',default='')


@contextmanager
def scope(conv):
    token=_current.set(conversation_key(conv))
    try:yield
    finally:_current.reset(token)


def scoped(resolve):
    def decorate(fn):
        @wraps(fn)
        def run(*a,**kw):
            with scope(resolve(*a,**kw)):return fn(*a,**kw)
        return run
    return decorate


PHRASES={
 ('brevity','short'):[r'(?:以后)?(?:回复|回话|回答|说话)?(?:不要|别)(?:写|回|说)?(?:得)?这么长(?:了)?',r'(?:以后)?(?:回复|回答|说话)?(?:再)?(?:简短|短)(?:一?点|一些)(?:吧)?',r'(?:这次|这条|刚才)?(?:回复|回答|回得|写得)?太长了',r'(?:以后)?(?:回复|回答)?少写点'],
 ('brevity','detailed'):[r'(?:以后)?(?:回复|回答|解释|说)?(?:详细|具体)(?:一?点|一些)(?:吧)?',r'(?:以后)?(?:别|不要)(?:回复|回答)?太简略(?:了)?'],
 ('emoji','less'):[r'(?:以后)?(?:回复|聊天|说话)?(?:少用|别用|不要用|不要发|不要)(?:这么多)?(?:表情|表情包|emoji)(?:了)?',r'(?:表情|表情包)太多了'],
 ('tone','natural'):[r'(?:以后)?(?:说话|回复|回答)?(?:自然|随意|口语化)(?:一?点|一些)(?:吧)?',r'(?:别|不要)(?:写得|说得|回复得)?(?:像客服一样|像客服|这么官方|这么生硬|这么客套)(?:了)?',r'(?:太官方了|太像客服了|少来套话|别说套话)'],
}
OUTPUTS={('brevity','short'):'本群日常回复简短自然，复杂问题仍保留必要信息。',
 ('brevity','detailed'):'本群需要解释时具体一些，保留关键原因。',
 ('emoji','less'):'本群回复少用表情，不堆表情包。',
 ('tone','natural'):'本群措辞自然口语化，少用客服式套话；不编造亲历或强行玩笑。'}


def extract(text):
    """Recognize complete natural feedback sentences; unsupported content never becomes rules."""
    text=text.strip()
    if len(text)>180 or any(x in text for x in ('他说','她说','有人说','转述','如果','假如','比如','例如','http','忽略','转账','密码','权限','“','”','「','」','"')):return []
    directed=bool(re.match(r'^(?:bot|机器人|分身)[，,:：\s]*',text,re.I))
    text=re.sub(r'^(?:bot|机器人|分身)[，,:：\s]*','',text,flags=re.I)
    local=any(x in text for x in ('这次','这条','仅这次','只改这句','就这次')) and '以后' not in text
    parts=[p.strip(' 。！!？?') for p in re.split('[，,；;。！!]',text) if p.strip(' 。！!？?')]
    results=[]
    for part in parts:
        clause_local=local or (any(x in part for x in ('这次','这条','仅这次','只改这句','就这次')) and '以后' not in part)
        if part in ('这句不像我','这个不像我的口气','这不像我说的话'):
            results.append({'key':'unspecified','value':'unknown','directed':directed,'local':local})
            continue
        matched=False
        for (key,value),patterns in PHRASES.items():
            if any(re.fullmatch(pattern,part,re.I) for pattern in patterns):
                results.append({'key':key,'value':value,'directed':directed,'local':clause_local});matched=True;break
        if not matched:
            # One unsupported clause makes the entire statement ambiguous; do not strip it away.
            if re.search(r'不像我|(?:以后|下次).{0,8}(?:回复|回答|说话)|(?:回复|回答).{0,4}(?:太|别|不要)',text):
                return [{'key':'unspecified','value':'unknown','directed':directed,'local':local}]
            return []
    specified=[r for r in results if r['key']!='unspecified']
    return specified or results


def init(db):
    schema='''
      CREATE TABLE IF NOT EXISTS owner_feedback_context(event_id TEXT PRIMARY KEY,bot_job TEXT);
      CREATE TABLE IF NOT EXISTS style_candidates(
        id TEXT PRIMARY KEY,event_id TEXT,conv TEXT,key TEXT,value TEXT,source_text TEXT,
        observed REAL,status TEXT,reason TEXT,bot_job TEXT,decision TEXT DEFAULT '');
      CREATE INDEX IF NOT EXISTS style_scope ON style_candidates(conv,key,observed);
    '''
    for statement in schema.split(';'):
        if statement.strip():db.execute(statement)


def bind_context(db,conv,observation,now):
    """Bind only the first new observation to an immediately preceding confirmed bot bubble."""
    init(db)
    from owner_memory import norm
    fresh={e['id'] for e in observation.get('events',[])}
    visible=observation.get('visible',[])
    jobs=list(db.execute("SELECT id,payload FROM jobs WHERE conv=? AND state='sent' AND updated BETWEEN ? AND ?",(conv,now-300,now)))
    for index,entry in enumerate(visible):
        if entry['id'] not in fresh or entry['message'].get('self') is not True:continue
        previous=visible[index-1]['message'] if index else {}
        anchor=''
        if previous.get('self') is True and previous.get('certain') is True:
            hits=[j['id'] for j in jobs if norm(json.loads(j['payload']).get('reply',''))==norm(previous.get('text','')) and norm(previous.get('text',''))]
            if len(hits)==1:anchor=hits[0]
        db.execute('INSERT OR IGNORE INTO owner_feedback_context VALUES(?,?)',(entry['id'],anchor))


def refresh(db,now):
    init(db)
    # Source revocation or retention expiry invalidates every derived candidate, including approvals.
    db.execute("UPDATE style_candidates SET status='revoked',reason='source unavailable' WHERE NOT EXISTS (SELECT 1 FROM owner_samples s WHERE s.event_id=style_candidates.event_id AND s.origin='owner_likely' AND s.observed>?)",(now-90*86400,))
    rows=db.execute("SELECT s.*,COALESCE(c.bot_job,'') bot_job FROM owner_samples s LEFT JOIN owner_feedback_context c ON c.event_id=s.event_id WHERE s.origin='owner_likely' AND s.observed>?",(now-90*86400,)).fetchall()
    for r in rows:
        items=extract(r['text'])
        for item in items:
            cid=hashlib.sha256((r['event_id']+'\0'+item['key']+'\0'+item['value']).encode()).hexdigest()
            old=db.execute('SELECT decision FROM style_candidates WHERE id=?',(cid,)).fetchone()
            decision=old['decision'] if old else ''
            status,reason='candidate','needs bot context or explicit owner approval'
            anchored=bool(r['bot_job'] and db.execute("SELECT 1 FROM jobs WHERE id=? AND conv=? AND state='sent'",(r['bot_job'],r['conv'])).fetchone())
            if decision=='reject':status,reason='rejected','owner rejected candidate'
            elif item['key']=='unspecified':reason='feedback has no concrete replacement preference'
            elif item['local']:status,reason='one_off','explicitly limited to this reply'
            elif item['directed'] or anchored or decision=='approve':status,reason='active','explicit bot direction' if item['directed'] else 'confirmed preceding bot reply' if anchored else 'owner approved candidate'
            db.execute('INSERT OR REPLACE INTO style_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (cid,r['event_id'],r['conv'],item['key'],item['value'],r['text'],r['observed'],status,reason,r['bot_job'],decision))
    # Newer accepted correction wins within this group and preference axis. Contradictory
    # values from one message stay candidates rather than choosing an arbitrary clause.
    for conv,key in db.execute("SELECT DISTINCT conv,key FROM style_candidates WHERE status='active'").fetchall():
        active=db.execute("SELECT * FROM style_candidates WHERE conv=? AND key=? AND status='active' ORDER BY observed DESC,id",(conv,key)).fetchall()
        conflicting={r['event_id'] for r in active if len({s['value'] for s in active if s['event_id']==r['event_id']})>1}
        for eid in conflicting:db.execute("UPDATE style_candidates SET status='candidate',reason='conflicting feedback in one message' WHERE event_id=? AND key=?",(eid,key))
        valid=[r for r in active if r['event_id'] not in conflicting]
        for r in valid[1:]:db.execute("UPDATE style_candidates SET status='superseded',reason='newer correction in this group' WHERE id=?",(r['id'],))
    db.execute('DELETE FROM style_candidates WHERE observed<?',(now-90*86400,))
    db.execute('DELETE FROM owner_feedback_context WHERE event_id NOT IN (SELECT event_id FROM owner_samples)')


def active(db,conv):
    init(db)
    if not conv:return {}
    rows=db.execute("SELECT c.* FROM style_candidates c JOIN owner_samples s ON s.event_id=c.event_id WHERE c.conv=? AND c.status='active' AND s.origin='owner_likely' AND s.observed>?",(conversation_key(conv),time.time()-90*86400)).fetchall()
    # Explicit manual preferences, including disabled ones, cannot be overridden by inference.
    manual={r[0] for r in db.execute("SELECT key FROM owner_preferences WHERE source='owner_explicit'")}
    return {r['key']:OUTPUTS[(r['key'],r['value'])] for r in rows if r['key'] not in manual and (r['key'],r['value']) in OUTPUTS}


def decide(outbox,cid,decision):
    if decision not in ('approve','reject'):raise ValueError('invalid decision')
    with outbox.connect() as db:
        init(db)
        if not db.execute('SELECT 1 FROM style_candidates WHERE id=?',(cid,)).fetchone():raise ValueError('candidate not found')
        db.execute('UPDATE style_candidates SET decision=? WHERE id=?',(decision,cid))
    # Separate transaction avoids schema helpers committing a half-built summary.
    from owner_memory import summarize
    summarize(outbox)
