#!/usr/bin/env python3
"""Inspect and explicitly manage learned preferences. No chat content printed."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox
import owner_memory
import natural_preferences
p=argparse.ArgumentParser();p.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'))
p.add_argument('--set',choices=['brevity','emoji','avoid_words']);p.add_argument('--value');p.add_argument('--evidence');p.add_argument('--disable',choices=['brevity','emoji','avoid_words'])
decisions=p.add_mutually_exclusive_group();decisions.add_argument('--approve');decisions.add_argument('--reject')
a=p.parse_args();out=Outbox(a.db)
if a.approve or a.reject:natural_preferences.decide(out,a.approve or a.reject,'approve' if a.approve else 'reject')
if a.set:
 if not a.value or not a.evidence:p.error('value and owner evidence required')
 owner_memory.set_preference(out,a.set,json.loads(a.value),'owner_explicit',a.evidence)
if a.disable:
 with out.connect() as db:
  owner_memory.init(db)
  db.execute("UPDATE owner_preferences SET active=0,source='owner_explicit' WHERE key=?",(a.disable,))
print(json.dumps(owner_memory.status(out),ensure_ascii=False,indent=2))
