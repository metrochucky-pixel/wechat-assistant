#!/usr/bin/env python3
"""Read-only end-to-end receipt report; never prints conversation text."""
import argparse,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reply_metrics import report
p=argparse.ArgumentParser();p.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'outbox.sqlite3'));p.add_argument('--hours',type=float,default=24)
a=p.parse_args()
with sqlite3.connect(Path(a.db).resolve().as_uri()+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    print(json.dumps(report(db,hours=a.hours),ensure_ascii=False,indent=2))
