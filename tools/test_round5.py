"""Regression for repeated recovery, gap lifecycle, visit budgets and offscreen discovery."""
import os,sys,tempfile,time,unittest,datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox,DeadlineExceeded
from conversation_planner import TurnPlanner
from test_conversation_turns import msg,action
from reply_metrics import report
from bot_scheduler import OffscreenPatrol,find_pending_page
import model_router
from PIL import Image


class Round5(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Outbox(self.temp.name+'/db');self.now=time.time();self.p=TurnPlanner(self.out,clock=lambda:self.now)
        self.conv='测试群';self.base=[msg('我','旧回复',self_=True)];self.p.ingest(self.conv,self.base)
    def new(self):
        self.base.append(msg('甲','新加坡硕士申请条件'))
        ob=self.p.ingest(self.conv,self.base);self.p.apply(self.conv,ob,[action('new',ob['events'])],False)
        return self.p.requests(self.conv)[0]
    def recovery(self):
        seen=self.p.ingest(self.conv,self.base)
        return self.p.recover_social(self.conv,seen,datetime.datetime.fromtimestamp(self.now).strftime('%Y-%m-%d %H:%M'))
    def test_closed_source_cannot_recover_under_new_timestamp(self):
        self.new()
        with self.out.connect() as db:db.execute("UPDATE turn_requests SET state='cancelled'")
        self.now+=60
        self.assertFalse(self.recovery()['events'])
    def test_ignored_recovery_done_by_event_not_timestamp(self):
        self.base.append(msg('甲','酒吧贵'));self.p.ingest(self.conv,self.base)
        # Mark normal ingestion planned to simulate an idle observed screen.
        with self.out.connect() as db:db.execute('UPDATE turn_reads SET planned=1')
        ob=self.recovery();self.assertTrue(ob['events'])
        self.p.apply(self.conv,ob,[action('ignore',ob['events'])],False,True)
        self.now+=60;self.assertFalse(self.recovery()['events'])
    def test_failed_recovery_has_two_attempt_budget(self):
        self.base.append(msg('甲','酒吧贵'));self.p.ingest(self.conv,self.base)
        with self.out.connect() as db:db.execute('UPDATE turn_reads SET planned=1')
        self.assertTrue(self.recovery()['events']);self.now+=60
        self.assertTrue(self.recovery()['events']);self.now+=60
        self.assertFalse(self.recovery()['events'])
    def test_old_gap_expires_without_reopening(self):
        r=self.new();self.p.ingest(self.conv,[msg('乙','全新画面无法衔接')]);self.now+=1801;self.p.maintain_gaps()
        with self.out.connect() as db:self.assertEqual(db.execute('SELECT state FROM turn_requests WHERE id=?',(r['id'],)).fetchone()[0],'expired')
    def test_recent_gap_is_target_and_visible_in_metrics(self):
        self.new();self.p.ingest(self.conv,[msg('乙','全新画面无法衔接')])
        self.assertIn(self.conv,self.out.targets())
        with self.out.connect() as db:self.assertEqual(len(report(db)['technical_review_candidates']),1)
    def test_two_spaced_observations_close_unverifiable_gap(self):
        self.new();self.p.ingest(self.conv,[msg('乙','全新画面无法衔接')])
        self.p.maintain_gaps(self.conv,True);self.p.maintain_gaps(self.conv,True)
        with self.out.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM turn_requests WHERE state='review'").fetchone()[0],1)
        self.now+=31;self.p.maintain_gaps(self.conv,True)
        with self.out.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM turn_requests WHERE state='review'").fetchone()[0],0)
    def test_uncertain_gap_send_preserved(self):
        self.new();self.now+=7;self.p.enqueue(self.conv,False);j=self.out.next(self.conv)
        self.out.update(j['id'],'uncertain');self.p.ingest(self.conv,[msg('乙','全新画面无法衔接')]);self.now+=3600;self.p.maintain_gaps()
        self.assertEqual(self.out.get(j['id'])['state'],'uncertain')
        with self.out.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM turn_requests WHERE state='review'").fetchone()[0],1)
    def test_fresh_request_after_gap_can_create_new_work(self):
        self.new();fresh=[msg('乙','全新画面无法衔接')];self.p.ingest(self.conv,fresh);self.now+=1801;self.p.maintain_gaps()
        ob=self.p.ingest(self.conv,fresh+[msg('甲','新加坡硕士申请条件请再回答一下')])
        self.p.apply(self.conv,ob,[action('new',ob['events'])],False)
        self.assertEqual(len(self.p.requests(self.conv)),1)
    def test_nested_reads_share_visit_budget(self):
        with model_router.task('visit'):
            for _ in range(5):
                with model_router.task('read'):self.assertLessEqual(model_router.timeout(99),10)
            with model_router.task('read'):
                with self.assertRaises(model_router.BudgetExceeded):model_router.timeout()
    def test_whole_visit_deadline_interrupts_wait(self):
        with patch.dict(model_router.PROFILES,{'visit':{'total':.02,'call':.01,'attempts':5}}):
            with self.assertRaises(DeadlineExceeded):
                with model_router.task('visit'):time.sleep(.2)
    def test_offscreen_delay_cooldown_and_mention_priority(self):
        clock=[1000];p=OffscreenPatrol(clock=lambda:clock[0]);rows=[{'name':'群A'}]
        self.assertEqual(p.choose(['群B'],rows),[]);clock[0]+=31
        self.assertEqual(p.choose(['群B'],[{'name':'群A','at_me':True}]),[])
        self.assertEqual(p.choose(['群B'],rows),['群B']);self.assertEqual(p.choose(['群B'],rows),[])
    def test_offscreen_returns_fresh_coordinates_only(self):
        a=Image.new('RGB',(10,10),'white');b=Image.new('RGB',(10,10),'blue');shots=iter([a,b]);swipes=[]
        result=find_pending_page(a,['群B'],lambda:next(shots),lambda im:[{'name':'群B','y':900}],swipes.append,lambda a,b:True)
        self.assertIs(result[0],b);self.assertEqual(result[1][0]['y'],900);self.assertEqual(len(swipes),1)
    def test_offscreen_rejects_changed_page_before_swipe(self):
        a=Image.new('RGB',(10,10));swipes=[]
        self.assertIsNone(find_pending_page(a,['群B'],lambda:a,lambda im:[],swipes.append,lambda a,b:False))
        self.assertEqual(swipes,[])
    def test_offscreen_page_limit(self):
        a=Image.new('RGB',(10,10));n=[0];swipes=[]
        def capture():
            n[0]+=1;return Image.new('RGB',(10,10),(n[0],0,0))
        result=find_pending_page(a,['群B'],capture,lambda im:[],swipes.append,lambda a,b:True)
        self.assertEqual(len(swipes),3);self.assertEqual(result[1],[])

if __name__=='__main__':unittest.main()
