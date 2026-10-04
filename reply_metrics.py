"""Receipt timings and explicit proxies. No phone operations or message content in reports."""
import json
import time


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reply_receipts(
        job_id TEXT PRIMARY KEY, conv TEXT, event TEXT, discovered REAL, confirmed REAL, basis TEXT)''')


def confirmed(db, jid, now):
    row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    if not row or row['state'] != 'sent':
        return
    d = json.loads(row['payload']); start = row['created']; basis = 'job_created'
    if d.get('_request_id') and db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_requests'").fetchone():
        r = db.execute('SELECT * FROM turn_requests WHERE id=? AND conv=?', (d['_request_id'],row['conv'])).fetchone()
        if r:
            start = r['created']; basis = 'request_observed'
            ids = json.loads(r['sources'])
            if ids:
                e = db.execute('SELECT MIN(observed) FROM turn_events WHERE conv=? AND id IN ('+','.join('?' for _ in ids)+')',(row['conv'],*ids)).fetchone()[0]
                if e is not None:
                    start = min(start,e); basis = 'message_observed'
    db.execute('INSERT OR IGNORE INTO reply_receipts VALUES(?,?,?,?,?,?)',
               (jid,row['conv'],d.get('message_event') or jid,start,now,basis))
    db.execute('DELETE FROM reply_receipts WHERE confirmed<?',(now-90*86400,))


def report(db, now=None, hours=24):
    now = time.time() if now is None else now
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    since = now-hours*3600
    rows = list(db.execute('SELECT * FROM reply_receipts WHERE confirmed>=?',(since,))) if 'reply_receipts' in tables else []
    times = sorted(max(0,r['confirmed']-r['discovered']) for r in rows)
    def percentile(p):
        import math
        return round(times[max(0,math.ceil(len(times)*p)-1)],2) if times else None
    duplicate = 0
    if rows:
        from collections import Counter
        duplicate = sum(n-1 for n in Counter((r['conv'],r['event']) for r in rows).values() if n>1)
    pending = []
    if 'turn_requests' in tables:
        for r in db.execute("SELECT * FROM turn_requests WHERE state='open' AND eligible<?",(now-120,)):
            # Owner decisions are not automatically answerable overdue work.
            if r['kind'] != 'owner':
                pending.append({'request_id':r['id'],'kind':r['kind'],'eligible_age_seconds':round(now-r['eligible'])})
    reviews = []
    if 'turn_requests' in tables:
        reviews=[{'request_id':r['id'],'kind':r['kind'],'reason':r['reason'],'age_seconds':round(now-r['relevant'])} for r in db.execute("SELECT * FROM turn_requests WHERE state='review' AND reason!='owner review'")]
    states = dict(db.execute('SELECT state,COUNT(*) FROM jobs WHERE updated>=? GROUP BY state',(since,)))
    technical = db.execute("SELECT COUNT(*) FROM jobs WHERE state='ignored' AND updated>=? AND error LIKE 'technical:%'",(since,)).fetchone()[0]
    return {'window_hours':hours,'confirmed_samples':len(times),'discovery_to_receipt_p50_seconds':percentile(.5),
        'discovery_to_receipt_p95_seconds':percentile(.95),'max_seconds':round(max(times),2) if times else None,
        'clock_basis_counts':{basis:sum(r['basis']==basis for r in rows) for basis in sorted({r['basis'] for r in rows})},
        'duplicate_event_receipts':duplicate,'overdue_candidates':pending,'recent_job_states':states,
        'technical_review_candidates':reviews,'technical_skips':technical,'missed_reply_rate':None,'incorrect_skip_rate':None,
        'limits':'计时从程序首次观察到消息或任务开始，不是群友发出时间。超期是待排查候选，漏答/误判率需要人工标注。重复统计仅覆盖确认回执。'}
