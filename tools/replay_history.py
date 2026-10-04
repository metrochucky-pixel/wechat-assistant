#!/usr/bin/env python3
"""Export content-free historical screen sequences and replay ordering invariants offline.

This tests the deterministic event layer, not whether a language model should reply.
No phone/API imports, no writes to the source database.
"""
import argparse,json,sqlite3,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox
from conversation_planner import TurnPlanner,clean_messages


def export(db):
    cases=[]
    for group,row in enumerate(db.execute('SELECT messages FROM turn_screens ORDER BY conv'),1):
        entries=json.loads(row[0]);messages=[x['message'] for x in entries]
        if not messages:continue
        clean_messages(messages)
        people={};texts={}
        def anon(text):
            if not text:return ''
            if text not in texts:texts[text]='脱敏消息编号%04d，保留顺序与重复关系'%(len(texts)+1)
            return texts[text]
        sanitized=[]
        for m in messages:
            if m['sender'] not in people:people[m['sender']]='成员%03d'%(len(people)+1)
            sanitized.append(dict(m,sender=people[m['sender']] if m['sender'] else '',
                text=('@Bot Example ' if m['direct_mention'] else '')+anon(m['text']),quote=anon(m['quote'])))
        cases.append({'conversation':'历史会话%03d'%group,'messages':sanitized})
    return {'version':1,'scope':'只保留消息顺序、重复关系、发送者区分和显式提及标记；不含原文，不评估模型语义判断。','cases':cases}


def replay(fixture):
    checks=0
    with tempfile.TemporaryDirectory() as directory:
        out=Outbox(directory+'/replay.sqlite');planner=TurnPlanner(out)
        for case in fixture['cases']:
            conv=case['conversation'];messages=clean_messages(case['messages'])
            # Baseline is never treated as newly received; repeated reads are idempotent.
            first=planner.ingest(conv,messages)
            assert not first['events'], 'baseline replayed'
            ids=[x['id'] for x in first['visible']]
            again=planner.ingest(conv,messages)
            assert [x['id'] for x in again['visible']]==ids,'same screen changed event identity'
            assert not again['events'],'same screen created work'
            checks+=3
            # Append a distinct new event to the actual historical tail and re-read it.
            extra={'sender':'回放新增成员','text':'回放新增问题，请按当前条件回答','quote':'','certain':True,'self':False,'direct_mention':False}
            updated=messages[-10:]+[extra]
            new=planner.ingest(conv,updated)
            assert len(new['events'])==1,'new tail not captured exactly once'
            again=planner.ingest(conv,updated)
            assert [e['id'] for e in again['events']]==[e['id'] for e in new['events']],'retry changed pending event'
            checks+=2
    return {'groups':len(fixture['cases']),'checks':checks,'result':'passed'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--db');p.add_argument('--fixture');p.add_argument('--export')
    a=p.parse_args()
    if a.db:
        with sqlite3.connect(Path(a.db).resolve().as_uri()+'?mode=ro',uri=True) as db:data=export(db)
        if a.export:Path(a.export).write_text(json.dumps(data,ensure_ascii=False,indent=2))
    elif a.fixture:data=json.loads(Path(a.fixture).read_text())
    else:p.error('provide --db or --fixture')
    print(json.dumps(replay(data),ensure_ascii=False))


if __name__=='__main__':main()
