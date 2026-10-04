import json,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
from owner_review import OwnerReview
from test_conversation_turns import msg,action

class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.o=Outbox(self.tmp.name+'/state.db');self.p=TurnPlanner(self.o)
        self.sent=[]
        def post(url,body,token):
            self.sent.append(body);return {'code':0,'data':{'chat_id':'owner-chat','message_id':'notice'}}
        self.r=OwnerReview(self.o,'owner',lambda:'token',post)
        self.conv='MentionOnly';base=[msg('me','old',self_=True)]
        self.p.ingest(self.conv,base);ob=self.p.ingest(self.conv,base+[msg('A','@Bot Example 来不来',True)])
        self.p.apply(self.conv,ob,[action('new',ob['events'],'A','今晚来吗')],True)
        with self.o.connect() as db:db.execute('UPDATE turn_requests SET eligible=0')
        self.p.enqueue(self.conv,True);self.j=self.o.next(self.conv)
        self.n=self.r.ask(self.j,'时间需要你决定','今晚不去');self.r.notify_pending()
    def reply(self,text,mid='reply',sender='owner',chat='owner-chat',created=None):
        return self.r.receive({'message_id':mid,'sender':{'id':sender,'sender_type':'user'},'msg_type':'text','body':{'content':json.dumps({'text':text})},'create_time':str(int((created or time.time())*1000))},chat)
    def test_notice_dedup_and_repeated_command(self):
        self.r.ask(self.j,'again');self.r.notify_pending();self.assertEqual(len(self.sent),1)
        result=self.reply(f'#{self.n} 改成 晚点给你答复');self.assertIn('恢复任务',result)
        self.assertIsNone(self.reply(f'#{self.n} 改成 别的内容'))
        j=self.o.get(self.j['id']);self.assertEqual(j['payload']['reply'],'晚点给你答复')
        self.assertTrue(self.p.valid(j,True));self.assertEqual(j['state'],'ready')
    def test_wrong_sender_chat_and_old_message_rejected(self):
        for kw in ({'sender':'stranger'},{'chat':'other'},{'created':time.time()-100}):
            self.assertIsNone(self.reply(f'#{self.n} 照发',**kw))
        self.assertEqual(self.o.get(self.j['id'])['state'],'human')
    def test_revision_change_rejects_approval(self):
        with self.o.connect() as db:db.execute('UPDATE turn_requests SET revision=revision+1')
        self.assertIn('已变化',self.reply(f'#{self.n} 照发'))
        self.assertEqual(self.o.get(self.j['id'])['state'],'human')
    def test_skip_cancels_and_does_not_send(self):
        self.reply(f'#{self.n} 我自己回');self.assertEqual(self.o.get(self.j['id'])['state'],'ignored')
        self.assertFalse(self.p.valid(self.o.get(self.j['id']),True))
    def test_supplement_requires_new_confirmation(self):
        self.reply(f'#{self.n} 补充 今晚另有安排')
        j=self.o.get(self.j['id']);self.assertTrue(j['payload']['_owner_reconfirm']);self.assertFalse(j['payload']['_answer_ready'])
        second=self.r.ask(j,'确认新拟稿','今晚有事 就不去了');self.assertNotEqual(self.n,second)
    def test_gap_after_approval_still_blocks_send(self):
        self.reply(f'#{self.n} 照发')
        self.p.ingest(self.conv,[msg('B','new unrelated screen')])
        self.assertFalse(self.p.valid(self.o.get(self.j['id']),True))
    def test_empty_draft_cannot_approve(self):
        with self.o.connect() as db:db.execute("UPDATE owner_questions SET draft='' WHERE id=?",(self.n,))
        self.assertIn('没有可照发',self.reply(f'#{self.n} 照发'))
    def test_correction_while_waiting_supersedes_confirmation(self):
        self.assertEqual(len(self.p.planning_requests(self.conv)),1)
        with self.o.connect() as db:
            screen=json.loads(db.execute('SELECT messages FROM turn_screens').fetchone()['messages'])
        messages=[x['message'] for x in screen]+[msg('A','改成明晚')]
        ob=self.p.ingest(self.conv,messages)
        self.p.apply(self.conv,ob,[action('update',ob['events'],'A','明晚来吗',rid=self.j['payload']['_request_id'])],True)
        self.r.refresh_changed()
        with self.o.connect() as db:
            rows=db.execute('SELECT state,question FROM owner_questions ORDER BY id').fetchall()
        self.assertEqual(rows[0]['state'],'superseded');self.assertEqual(rows[1]['question'],'明晚来吗')
        self.assertEqual(self.o.get(self.j['id'])['state'],'human')
    def test_cancellation_while_waiting_cannot_be_resumed(self):
        with self.o.connect() as db:
            screen=json.loads(db.execute('SELECT messages FROM turn_screens').fetchone()['messages'])
        ob=self.p.ingest(self.conv,[x['message'] for x in screen]+[msg('A','不用来了 取消')])
        self.p.apply(self.conv,ob,[action('cancel',ob['events'],'A',rid=self.j['payload']['_request_id'])],True)
        self.r.refresh_changed();self.reply(f'#{self.n} 照发')
        self.assertEqual(self.o.get(self.j['id'])['state'],'ignored')

    def test_failed_notification_remains_retryable(self):
        with self.o.connect() as db:db.execute('UPDATE owner_questions SET notified=0')
        self.r.post=lambda *a:{'code':999}
        self.r.notify_pending()
        with self.o.connect() as db:
            q=db.execute('SELECT * FROM owner_questions').fetchone();self.assertEqual(q['notified'],0);self.assertGreater(q['due'],time.time())

if __name__=='__main__':unittest.main()
