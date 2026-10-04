#!/usr/bin/env python3
"""Read task status/latency without exposing prompts or operating the phone."""
import argparse,json,sqlite3,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'))
a=p.parse_args()
with sqlite3.connect('file:'+str(Path(a.db).resolve())+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='background_work'").fetchone():
        for row in db.execute('''SELECT j.display,w.phase,w.route,w.state,w.error,w.created
                FROM background_work w LEFT JOIN jobs j ON j.id=w.job_id ORDER BY w.created DESC LIMIT 20'''):
            d=dict(row);d['age_seconds']=round(time.time()-d.pop('created'));print(json.dumps(d,ensure_ascii=False))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='model_health'").fetchone():
        for row in db.execute('SELECT * FROM model_health ORDER BY task,provider'):
            print(json.dumps(dict(row),ensure_ascii=False))
