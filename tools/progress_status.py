#!/usr/bin/env python3
"""Read durable conversation failures without loading the bot or touching the phone."""
import argparse
import json
import sqlite3
import time
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--db', default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'))
a = p.parse_args()
with sqlite3.connect('file:'+str(Path(a.db).resolve())+'?mode=ro', uri=True) as db:
    db.row_factory = sqlite3.Row
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_progress'").fetchone():
        print('尚无持续故障记录')
    else:
        for row in db.execute('SELECT * FROM conversation_progress ORDER BY updated DESC LIMIT 100'):
            item = dict(row)
            item['remaining_seconds'] = max(0, round(item.pop('until')-time.time()))
            print(json.dumps(item, ensure_ascii=False))
