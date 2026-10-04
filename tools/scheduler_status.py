#!/usr/bin/env python3
"""Read-only scheduler counters; no model or phone access."""
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'))
a=p.parse_args()
with sqlite3.connect('file:'+str(Path(a.db).resolve())+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_counts'").fetchone():
        print('尚无调度计数')
    else:
        for row in db.execute('SELECT * FROM scheduler_counts ORDER BY day DESC,kind,conv LIMIT 200'):
            print(json.dumps(dict(row),ensure_ascii=False))
