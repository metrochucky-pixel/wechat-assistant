#!/usr/bin/env python3
"""Read-only latency summary. Does not import bot or access the phone/API."""
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'));p.add_argument('--limit',type=int,default=20)
a=p.parse_args()
c=sqlite3.connect('file:'+str(Path(a.db).resolve())+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
if not c.execute("select 1 from sqlite_master where name='performance_visits'").fetchone():
 print('尚无访问耗时记录');raise SystemExit(0)
for row in c.execute('select * from performance_visits order by id desc limit ?', (max(1,min(a.limit,200)),)):
 d=json.loads(row['payload']);routes=[e['value'] for e in d['events'] if e['kind']=='route']
 reasons=[e['value'] for e in d['events'] if e['kind'] in ('read_deferred','plan','provider_cooldown','delivery_error','preflight_error','draft_cleanup')]
 print(json.dumps({'id':row['id'],'group':row['display'],'seconds':round(row['total_ms']/1000,2),
 'outcome':row['outcome'],'action':d['action'],'api_calls':d['api_calls'],'cache_hits':d['cache_hits'],
 'routes':routes,'reasons':reasons,'stages':d['stages']},ensure_ascii=False))
