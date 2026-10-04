#!/usr/bin/env python3
"""Read-only queue health. Does not open the phone or call a model API."""
import json
import sqlite3
import time
from pathlib import Path
path = Path(__file__).resolve().parents[1] / 'outbox.sqlite3'
if not path.exists():
    print(json.dumps({'queue_exists': False})); raise SystemExit(0)
db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
db.row_factory = sqlite3.Row
counts = {r['state']: r['n'] for r in db.execute('SELECT state,count(*) AS n FROM jobs GROUP BY state')}
pending = [dict(r) for r in db.execute("SELECT display,state,attempts,created,due,error FROM jobs WHERE state NOT IN ('sent','ignored') ORDER BY created")]
for r in pending:
    r['age_seconds'] = int(time.time() - r.pop('created'))
    r['retry_in_seconds'] = max(0, int(r.pop('due') - time.time()))
print(json.dumps({'states':counts, 'pending':pending,
    'pending_observations': [dict(r) for r in db.execute('SELECT display,due FROM observations')],
    'pending_mentions': [r[0] for r in db.execute('SELECT display FROM mentions')],
    'provider_cooldowns': [dict(r) for r in db.execute('SELECT name,until,error FROM providers WHERE until>?', (time.time(),))]}, ensure_ascii=False, indent=2))
db.close()
