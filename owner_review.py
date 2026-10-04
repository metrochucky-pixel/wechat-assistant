"""Durable owner review. Only authenticated owner DM commands can resume a paused job."""
import json
import re
import time
import uuid
from urllib.parse import urlencode
import urllib.request


class OwnerReview:
    def __init__(self, outbox, owner_id, token, post):
        self.outbox, self.owner_id, self.token, self.post = outbox, owner_id, token, post
        with outbox.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS owner_questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, revision INTEGER,
                display TEXT, question TEXT, reason TEXT, draft TEXT, state TEXT DEFAULT 'pending',
                created REAL, notified REAL DEFAULT 0, chat_id TEXT DEFAULT '', message_id TEXT DEFAULT '',
                send_uuid TEXT, answer TEXT DEFAULT '', due REAL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS owner_receipts(message_id TEXT PRIMARY KEY, result TEXT, created REAL);
              CREATE TABLE IF NOT EXISTS owner_meta(key TEXT PRIMARY KEY,value TEXT);
            ''')

    def ask(self, job, reason, draft=''):
        d = job['payload']; now = time.time()
        with self.outbox.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if not current or current['state'] in ('sent','ignored','sending','uncertain'):
                return None
            existing=db.execute("SELECT id FROM owner_questions WHERE job_id=? AND state='pending'",(job['id'],)).fetchone()
            if existing:return existing['id']
            db.execute('INSERT INTO owner_questions(job_id,revision,display,question,reason,draft,created,send_uuid) VALUES(?,?,?,?,?,?,?,?)',
                (job['id'],d.get('_request_revision',0),job['display'],d.get('question_to_answer') or d.get('last_customer_msg') or '',reason,draft,now,str(uuid.uuid4())))
            db.execute("UPDATE jobs SET state='human',error=?,updated=? WHERE id=?",('owner review: '+reason,now,job['id']))
            db.execute("UPDATE turn_requests SET state='review',reason='owner review' WHERE id=? AND revision=?",(d.get('_request_id'),d.get('_request_revision')))
            return db.execute('SELECT id FROM owner_questions WHERE job_id=? ORDER BY id DESC LIMIT 1',(job['id'],)).fetchone()['id']

    def notify_pending(self):
        with self.outbox.connect() as db:
            rows = db.execute("SELECT * FROM owner_questions WHERE state='pending' AND notified=0 AND due<=? ORDER BY id LIMIT 3",(time.time(),)).fetchall()
        for row in rows:
            q = dict(row); number = '#%03d' % q['id']
            actions = (f'{number} 照发／' if q['draft'] else '') + f'{number} 改成 你的原话／{number} 补充 背景信息／{number} 跳过／{number} 我自己回'
            text = (f"待确认 {number}｜{q['display']}\n问题：{q['question'][:1600]}\n需要你判断：{q['reason'][:600]}\n"
                    f"拟回复：{q['draft'][:1600] or '尚未生成，不能直接照发'}\n\n"
                    f"直接回复：{actions}\n"
                    "补充只用于本次事项，重新拟稿后会再请你确认。没有答复就保持挂起。")
            try:
                result = self.post('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                    {'receive_id':self.owner_id,'msg_type':'text','uuid':q['send_uuid'],
                     'content':json.dumps({'text':text},ensure_ascii=False)},self.token())
                if result.get('code') != 0:raise RuntimeError('Feishu code '+str(result.get('code')))
                data=result['data']
                with self.outbox.connect() as db:
                    db.execute('UPDATE owner_questions SET notified=?,chat_id=?,message_id=? WHERE id=?',
                               (time.time(),data['chat_id'],data['message_id'],q['id']))
                print('[owner] notified '+number,flush=True)
            except Exception as exc:
                with self.outbox.connect() as db:db.execute('UPDATE owner_questions SET due=? WHERE id=?',(time.time()+60,q['id']))
                print('[owner] notify failed '+type(exc).__name__,flush=True)

    def receive(self, item, chat_id):
        sender=item.get('sender') or {}
        if sender.get('id') != self.owner_id or sender.get('sender_type') != 'user' or item.get('msg_type') != 'text' or item.get('deleted'):
            return None
        try:
            text=json.loads(item['body']['content'])['text'].strip()
            m=re.fullmatch(r'[#＃](\d+)\s*(照发|改成|补充|跳过|我自己回|收到)\s*[：:]?\s*([\s\S]*)',text)
            if not m:return None
            number,command,value=int(m[1]),m[2],m[3].strip()
            created=int(item['create_time'])/1000
            mid=item['message_id']
        except (ValueError,KeyError,TypeError):return None
        with self.outbox.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM owner_receipts WHERE message_id=?',(mid,)).fetchone():return None
            q=db.execute('SELECT * FROM owner_questions WHERE id=?',(number,)).fetchone()
            if not q or q['chat_id'] != chat_id or int(created*1000) < int(q['created']*1000):return None
            result='事项已经处理，无需重复回复'
            if q['state']=='pending':
                if q['job_id'].startswith('test:'):
                    result='收发验证成功；没有向微信群发送消息'
                    db.execute("UPDATE owner_questions SET state='resolved',answer=? WHERE id=?",(text,number))
                else:
                    result=self._resolve(db,q,command,value,text)
            db.execute('INSERT INTO owner_receipts VALUES(?,?,?)',(mid,result,time.time()))
        return '#%03d %s' % (number,result)

    def _resolve(self, db, q, command, value, text):
        j=db.execute('SELECT * FROM jobs WHERE id=?',(q['job_id'],)).fetchone()
        if not j or j['state']!='human':return '任务状态已变化，未恢复发送'
        d=json.loads(j['payload']);rid=d.get('_request_id')
        r=db.execute('SELECT * FROM turn_requests WHERE id=?',(rid,)).fetchone()
        if not r or r['revision']!=q['revision'] or r['state']!='review' or r['reason']!='owner review':
            return '群聊事项已变化，需重新核对；未恢复发送'
        if d.get('_owned_draft'):return '手机里已有草稿，需要现场核对；未恢复发送'
        if command in ('跳过','我自己回'):
            db.execute("UPDATE jobs SET state='ignored',error='owner declined',updated=? WHERE id=?",(time.time(),j['id']))
            db.execute("UPDATE turn_requests SET state='cancelled',reason='owner declined' WHERE id=?",(rid,))
        elif command in ('照发','改成','补充'):
            answer=q['draft'] if command=='照发' else value
            if not answer:return '请提供具体内容；当前没有可照发的拟回复'
            if len(answer)>6000:return '内容太长，请缩短到 6000 字内'
            # Never revive stale questions silently or overwrite a newer revision.
            if time.time()-q['created']>86400:return '事项超过一天，请重新核对当前群聊；未恢复发送'
            d['needs_human']=False;d.pop('_needs_owner',None)
            d['_owner_approved']=True;d['_owner_ticket']=q['id']
            if command=='补充':
                d['question_context']=d.get('question_context','')+'\n机主仅针对本事项补充：'+answer
                d['_needs_turn_draft']=True;d['is_deep_q']=False;d['_answer_ready']=False
                d['_owner_reconfirm']=True;d['reply']=''
            else:
                d['reply']=answer;d['_answer_ready']=True;d['_needs_turn_draft']=False
                d.pop('_sources',None);d.pop('_verified_at',None)
                d['wants_image']=False;d['offer_image']=False;d['needs_image']=False
                d.pop('_owner_reconfirm',None)
            db.execute("UPDATE turn_requests SET state='open',reason='owner supplied answer',eligible=?,relevant=? WHERE id=?",(time.time(),time.time(),rid))
            db.execute("UPDATE jobs SET state='ready',payload=?,due=?,updated=?,error='' WHERE id=?",(json.dumps(d,ensure_ascii=False),time.time(),time.time(),j['id']))
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)',(j['conv'],j['display'],time.time()))
        else:return '请使用照发、改成、补充、跳过或我自己回'
        db.execute("UPDATE owner_questions SET state='resolved',answer=? WHERE id=?",(text,q['id']))
        return '已记录；恢复任务后会核对群聊最新内容再发送' if command in ('照发','改成') else ('已记录补充，拟稿后再请你确认' if command=='补充' else '已停止此条自动回复')

    def poll(self):
        with self.outbox.connect() as db:
            chats=db.execute("SELECT chat_id,MIN(created) AS since FROM owner_questions WHERE chat_id<>'' GROUP BY chat_id").fetchall()
        for chat in chats:
            with self.outbox.connect() as db:
                row=db.execute('SELECT value FROM owner_meta WHERE key=?',('cursor:'+chat['chat_id'],)).fetchone()
            start=float(row['value']) if row else chat['since']-1
            end=time.time();page=''
            while True:
                params={'container_id_type':'chat','container_id':chat['chat_id'],'page_size':50,
                        'sort_type':'ByCreateTimeAsc','start_time':str(int(start)),'end_time':str(int(end))}
                if page:params['page_token']=page
                req=urllib.request.Request('https://open.feishu.cn/open-apis/im/v1/messages?'+urlencode(params),headers={'Authorization':'Bearer '+self.token()})
                with urllib.request.urlopen(req,timeout=15) as response:result=json.load(response)
                if result.get('code')!=0:raise RuntimeError('Feishu read code '+str(result.get('code')))
                data=result['data']
                for item in data.get('items',[]):
                    result=self.receive(item,chat['chat_id'])
                    if result:
                        self.post('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                            {'receive_id':self.owner_id,'msg_type':'text','uuid':str(uuid.uuid5(uuid.NAMESPACE_URL,item['message_id'])),
                             'content':json.dumps({'text':result},ensure_ascii=False)},self.token())
                        print('[owner] command processed',flush=True)
                if not data.get('has_more'):break
                page=data['page_token']
            with self.outbox.connect() as db:db.execute('INSERT OR REPLACE INTO owner_meta VALUES(?,?)',('cursor:'+chat['chat_id'],str(int(end)-1)))

    def refresh_changed(self):
        renew=[]
        with self.outbox.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM owner_questions WHERE state='pending'").fetchall()
            for q in rows:
                j=db.execute('SELECT * FROM jobs WHERE id=?',(q['job_id'],)).fetchone()
                if not j or j['state']!='human':continue
                d=json.loads(j['payload'])
                r=db.execute('SELECT * FROM turn_requests WHERE id=?',(d.get('_request_id'),)).fetchone()
                if not r:continue
                if r['state'] in ('cancelled','cancel','resolved','sent','expired'):
                    db.execute("UPDATE owner_questions SET state='cancelled' WHERE id=?",(q['id'],))
                    db.execute("UPDATE jobs SET state='ignored',error='request closed while waiting for owner' WHERE id=?",(j['id'],))
                elif r['revision']!=q['revision'] and r['state']=='review' and r['reason']=='owner review':
                    db.execute("UPDATE owner_questions SET state='superseded' WHERE id=?",(q['id'],))
                    if d.get('_owned_draft'):continue
                    d.update(_request_revision=r['revision'],question_to_answer=r['question'],
                             question_context=r['context'],reply='',_answer_ready=False,_owner_approved=False)
                    db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(d,ensure_ascii=False),j['id']))
                    renew.append(j['id'])
        for jid in renew:self.ask(self.outbox.get(jid),'群里补充或修改了问题，请按最新内容确认')

    def run(self):
        while True:
            try:
                self.refresh_changed();self.notify_pending();self.poll()
                with self.outbox.connect() as db:db.execute('INSERT OR REPLACE INTO owner_meta VALUES(?,?)',('heartbeat',str(time.time())))
            except Exception as exc:print('[owner] poll failed '+type(exc).__name__,flush=True)
            time.sleep(15)
