"""Durable computation queue. Workers never own the phone or write reply jobs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from model_router import PROFILES
import runtime_settings


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class BackgroundWork:
    def __init__(self,outbox,root,worker=None,limit=2):
        self.outbox=outbox;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.worker=worker or str(Path(__file__).with_name('background_worker.py'))
        self.limit=limit;self.running={};self.lock=threading.RLock();self.stop=threading.Event()
        with outbox.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS background_work (
                id TEXT PRIMARY KEY,job_id TEXT,phase TEXT,base_hash TEXT,route TEXT,
                state TEXT,created REAL,started REAL,token TEXT,result TEXT,error TEXT,
                input_path TEXT,output_path TEXT,priority INTEGER)''')
            abandoned=db.execute("SELECT input_path FROM background_work WHERE state IN ('queued','running') OR created<?",(time.time()-7*86400,)).fetchall()
            db.execute("UPDATE background_work SET state='failed',error='worker_restart' WHERE state IN ('queued','running')")
            db.execute("DELETE FROM background_work WHERE created<? AND state NOT IN ('queued','running')",(time.time()-7*86400,))
        import shutil
        for row in abandoned:
            folder=Path(row['input_path']).parent
            if folder.parent==self.root:shutil.rmtree(folder,ignore_errors=True)

    def valid(self,row):
        job=self.outbox.get(row['job_id'])
        if not job or job['state'] not in ('pending','ready','prepared'):return False
        if digest(job['payload'])!=row['base_hash']:return False
        d=job['payload']
        if d.get('_request_id'):
            with self.outbox.connect() as db:
                req=db.execute('SELECT state,revision,authorization FROM turn_requests WHERE id=?',(d['_request_id'],)).fetchone()
            if not req or req['state']!='open' or req['revision']!=d.get('_request_revision'):return False
            if runtime_settings.mention_only(job['conv']) and not json.loads(req['authorization']):return False
        return True

    def status(self,job,phase):
        key=hashlib.sha256((job['id']+phase+digest(job['payload'])).encode()).hexdigest()
        with self.outbox.connect() as db:row=db.execute('SELECT * FROM background_work WHERE id=?',(key,)).fetchone()
        if not row:return None
        if not self.valid(row):return ('stale',{})
        if row['state']=='done' and time.time()-row['created']>180:return ('failed',{'error':'result_expired'})
        return (row['state'],json.loads(row['result'] or '{}'))

    def submit(self,job,phase,route,inputs):
        with self.lock:
            status=self.status(job,phase)
            if status:return status
            base=digest(job['payload']);key=hashlib.sha256((job['id']+phase+base).encode()).hexdigest()
            token=uuid.uuid4().hex
            with self.outbox.connect() as db:
                if db.execute("SELECT count(*) FROM background_work WHERE state IN ('queued','running')").fetchone()[0]>=8:
                    return ('busy',{})
            # A unique private directory also contains any copied image evidence.
            folder=self.root/token;folder.mkdir(mode=0o700)
            def save(value):
                if hasattr(value,'save') and hasattr(value,'size'):
                    path=folder/(uuid.uuid4().hex+'.png');value.save(path);os.chmod(path,0o600)
                    return {'image_path':str(path)}
                if isinstance(value,dict):return {k:save(v) for k,v in value.items()}
                if isinstance(value,list):return [save(v) for v in value]
                return value
            request={'phase':phase,'route':route,'payload':job['payload'],'display':job['display'],'inputs':save(inputs)}
            inp=folder/'input.json';out=folder/'output.json'
            inp.write_text(json.dumps(request,ensure_ascii=False));os.chmod(inp,0o600)
            with self.outbox.connect() as db:
                db.execute('INSERT INTO background_work VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key,job['id'],phase,base,route,'queued',time.time(),0,token,'','',str(inp),str(out),job['payload'].get('_priority',1)))
            return ('queued',{})

    def tick(self):
        with self.lock:
            with self.outbox.connect() as db:
                rows=db.execute("SELECT * FROM background_work WHERE state IN ('queued','running') ORDER BY priority,created").fetchall()
            for row in rows:
                proc=self.running.get(row['id'])
                valid=self.valid(row);expired=time.time()-row['created']>180
                budget_over=proc is not None and time.time()-row['started']>PROFILES[row['route']]['total']+5
                if not valid or expired or budget_over:
                    if proc:proc.kill();proc.wait(timeout=2);self.running.pop(row['id'],None)
                    self.finish(row,'stale' if not valid else 'failed',{},'stale_input' if not valid else 'deadline')
                    continue
                if proc is not None:
                    if proc.poll() is None:continue
                    self.running.pop(row['id'],None)
                    try:
                        output=Path(row['output_path'])
                        if output.stat().st_size>2_000_000:raise ValueError('oversized result')
                        result=json.loads(output.read_text())
                        if not isinstance(result,dict) or not isinstance(result.get('data',{}),dict):raise ValueError('invalid result')
                        self.finish(row,'done' if result.get('ok') is True else 'failed',result.get('data',{}),result.get('error',''))
                    except Exception as exc:self.finish(row,'failed',{},type(exc).__name__)
                elif row['state']=='queued' and len(self.running)<self.limit:
                    env=dict(os.environ,WECHATBOT_BACKGROUND_WORKER='1',WECHATBOT_OUTBOX=self.outbox.path,WECHATBOT_PARENT_PID=str(os.getpid()))
                    try:
                        proc=subprocess.Popen([sys.executable,self.worker,row['input_path'],row['output_path']],env=env,
                                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                        self.running[row['id']]=proc
                        with self.outbox.connect() as db:db.execute("UPDATE background_work SET state='running',started=? WHERE id=?",(time.time(),row['id']))
                    except Exception as exc:self.finish(row,'failed',{},type(exc).__name__)

    def finish(self,row,state,result,error):
        if state=='done' and not self.valid(row):state='stale';result={}
        with self.outbox.connect() as db:
            db.execute('UPDATE background_work SET state=?,result=?,error=? WHERE id=? AND token=?',
                       (state,json.dumps(result,ensure_ascii=False),str(error)[:100],row['id'],row['token']))
            if state in ('done','failed'):
                db.execute("UPDATE jobs SET due=MIN(due,?) WHERE id=? AND state IN ('pending','ready','prepared')",(time.time(),row['job_id']))
        # Input screenshots/prompts are no longer needed after computation.
        import shutil
        shutil.rmtree(Path(row['input_path']).parent,ignore_errors=True)
        print('[background] %s %s %s' % (row['phase'],state,str(error)[:50]))

    def start(self):
        def run():
            while not self.stop.is_set():
                try:self.tick()
                except Exception as exc:print('[background] supervisor '+type(exc).__name__)
                self.stop.wait(.5)
        threading.Thread(target=run,name='answer-supervisor',daemon=True).start()

    def close(self):
        self.stop.set()
        with self.lock:
            for proc in self.running.values():
                if proc.poll() is None:proc.kill()
                proc.wait(timeout=2)
            self.running.clear()
