"""Bounded subprocess failure injection; no model calls or phone access."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from background_work import BackgroundWork,digest
from bot_reliability import Outbox,DeadlineExceeded,WebAnswer
import model_router
_import=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_import.name+'/import.sqlite'
import wechat_autoreply as b

FAKE='''import json,sys,time
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text());time.sleep(r['inputs'].get('sleep',0))
Path(sys.argv[2]).write_text(json.dumps({'ok':True,'data':{'reply':'answer','evidence':{'_deep_status':'returned'}}}))
'''


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.out=Outbox(self.tmp.name+'/db');worker=Path(self.tmp.name)/'worker.py';worker.write_text(FAKE)
        self.bg=BackgroundWork(self.out,self.tmp.name+'/evidence',worker=str(worker));self.addCleanup(self.bg.close)
    def job(self,name='A',**fields):return self.out.add(name,dict(last_customer_msg='question',reply='',**fields))
    def wait(self,job,phase='knowledge'):
        end=time.time()+3
        while time.time()<end:
            self.bg.tick();status=self.bg.status(job,phase)
            if status and status[0] not in ('queued','running'):return status
            time.sleep(.02)
        self.fail('worker did not complete')
    def test_slow_job_does_not_block_fast_group(self):
        slow=self.job('slow');fast=self.job('fast')
        start=time.monotonic();self.bg.submit(slow,'knowledge','knowledge',{'sleep':.8});self.bg.submit(fast,'knowledge','knowledge',{})
        self.assertLess(time.monotonic()-start,.2)
        self.assertEqual(self.wait(fast)[0],'done')
        self.assertEqual(self.bg.status(slow,'knowledge')[0],'running')
    def test_deduplication_and_process_limit(self):
        jobs=[self.job(str(i)) for i in range(3)]
        for job in jobs:self.bg.submit(job,'knowledge','knowledge',{'sleep':1})
        self.bg.submit(jobs[0],'knowledge','knowledge',{'sleep':1});self.bg.tick()
        self.assertEqual(len(self.bg.running),2)
        with self.out.connect() as db:self.assertEqual(db.execute('select count(*) from background_work').fetchone()[0],3)
    def test_queue_capacity_is_bounded(self):
        for i in range(8):self.bg.submit(self.job(str(i)),'knowledge','knowledge',{})
        self.assertEqual(self.bg.submit(self.job('ninth'),'knowledge','knowledge',{})[0],'busy')
    def test_changed_question_discards_old_result(self):
        job=self.job();self.bg.submit(job,'knowledge','knowledge',{'sleep':.1});self.bg.tick()
        d=dict(job['payload'],question_to_answer='changed');self.out.update(job['id'],'pending',d)
        self.bg.tick();self.assertEqual(self.bg.status(job,'knowledge')[0],'stale')
        self.assertEqual(self.out.get(job['id'])['payload']['reply'],'')
    def test_cancelled_or_uncertain_jobs_never_accept_output(self):
        for state in ('ignored','human','uncertain','sending'):
            job=self.job(state);self.bg.submit(job,'knowledge','knowledge',{})
            self.out.update(job['id'],state);self.bg.tick()
            self.assertEqual(self.bg.status(job,'knowledge')[0],'stale')
    def test_supervisor_kills_overdue_process(self):
        job=self.job();self.bg.submit(job,'knowledge','chat',{'sleep':10});self.bg.tick()
        with self.out.connect() as db:db.execute('UPDATE background_work SET started=?',(time.time()-100,))
        self.bg.tick();self.assertEqual(self.bg.status(job,'knowledge')[0],'failed');self.assertEqual(len(self.bg.running),0)
    def test_restart_does_not_accept_unfinished_output(self):
        job=self.job();self.bg.submit(job,'knowledge','knowledge',{})
        restarted=BackgroundWork(self.out,self.tmp.name+'/evidence');self.addCleanup(restarted.close)
        self.assertEqual(restarted.status(job,'knowledge')[0],'failed')
    def test_finished_job_does_not_modify_reply_transaction(self):
        job=self.job();self.bg.submit(job,'knowledge','knowledge',{})
        self.assertEqual(self.wait(job)[0],'done')
        current=self.out.get(job['id']);self.assertEqual(current['state'],'pending');self.assertEqual(current['payload'],job['payload'])
    def test_result_expiry_and_private_evidence_cleanup(self):
        job=self.job();self.bg.submit(job,'knowledge','knowledge',{})
        files=list(Path(self.tmp.name+'/evidence').glob('*/input.json'));self.assertEqual(files[0].stat().st_mode&0o777,0o600)
        self.wait(job);self.assertFalse(files[0].exists())
        with self.out.connect() as db:db.execute('UPDATE background_work SET created=?',(time.time()-181,))
        self.assertEqual(self.bg.status(job,'knowledge')[0],'failed')
    def test_worker_phone_boundary(self):
        with patch.dict(os.environ,{'WECHATBOT_BACKGROUND_WORKER':'1'}),patch.object(b.subprocess,'run') as call:
            with self.assertRaises(RuntimeError):b.adb('shell','input','tap','1','1')
            call.assert_not_called()
    def test_request_revision_or_cancellation_invalidates_unchanged_job(self):
        from conversation_planner import TurnPlanner,POLICY
        planner=TurnPlanner(self.out)
        # Only the columns read by the fence are required; use the actual table.
        with self.out.connect() as db:
            columns={r['name']:r for r in db.execute('PRAGMA table_info(turn_requests)')}
            values={k:('open' if k=='state' else '[]' if k=='authorization' else 1 if k=='revision' else 0 if r['type'] in ('REAL','INTEGER') else 'test') for k,r in columns.items()}
            values['id']='req';values['conv']='a'
            db.execute('INSERT INTO turn_requests('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
        job=self.job(_request_id='req',_request_revision=1,_turn_policy=POLICY)
        self.bg.submit(job,'knowledge','knowledge',{})
        with self.out.connect() as db:db.execute("UPDATE turn_requests SET revision=2 WHERE id='req'")
        self.bg.tick();self.assertEqual(self.bg.status(job,'knowledge')[0],'stale')
    def test_delivery_returns_while_background_computes(self):
        job=self.job(is_deep_q=True,question_to_answer='question',question_context='')
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'BACKGROUND',self.bg),patch.object(b.TURNS,'valid',return_value=True),patch.object(b,'_breaker_blocked',return_value=False),patch.object(b,'send_reply') as send:
            result=b._deliver_job(job,set())
            self.assertEqual(result['_action'],'待答_后台生成中');send.assert_not_called()
    def test_ready_result_still_passes_existing_send_path(self):
        job=self.job(is_deep_q=True,question_to_answer='question',question_context='')
        self.bg.submit(job,'knowledge','knowledge',{});self.wait(job)
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'BACKGROUND',self.bg),patch.object(b.TURNS,'valid',return_value=True),patch.object(b,'_breaker_blocked',return_value=False),patch.object(b,'DRY_RUN',False),patch.object(b,'send_reply',return_value=True) as send,patch.object(b,'_complete_job'):
            result=b._deliver_job(job,set())
            self.assertEqual(result['_action'],'已发送');send.assert_called_once()


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.out=Outbox(self.tmp.name+'/db')
    def test_each_profile_has_call_and_attempt_budget(self):
        for kind,profile in model_router.PROFILES.items():
            with model_router.task(kind):
                for _ in range(profile['attempts']):self.assertLessEqual(model_router.timeout(100),profile['call'])
                with self.assertRaises(model_router.BudgetExceeded):model_router.timeout()
    def test_nested_visual_read_does_not_reset_parent_budget(self):
        with model_router.task('chat'):
            model_router.timeout()
            with model_router.task('read'):model_router.timeout()
            with self.assertRaises(model_router.BudgetExceeded):model_router.timeout()
    def test_total_wall_budget_interrupts_hung_call(self):
        with patch.dict(model_router.PROFILES,{'test':dict(total=.01,call=.01,attempts=1)}):
            with self.assertRaises(DeadlineExceeded):
                with model_router.task('test'):time.sleep(.2)
    def test_recent_latency_and_errors_change_order(self):
        with model_router.task('chat'):
            model_router.record(self.out,'grok',18,False);model_router.record(self.out,'m3',2,True)
        self.assertEqual(model_router.order(self.out,'chat',['grok','m3'])[0],'m3')
        self.out.provider_failed('m3',TimeoutError())
        self.assertEqual(model_router.order(self.out,'chat',['grok','m3']),['grok'])
    def test_search_candidates_do_not_include_plain_text_models(self):
        self.assertEqual(set(model_router.order(self.out,'knowledge',['grok-search','claude'])),{'grok-search','claude'})
    def test_current_question_goes_directly_to_sourced_search(self):
        answer=WebAnswer('verified',['https://example.org/source'],'test')
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'requires_current_sources',return_value=True),patch.object(b,'_grok_web',return_value=answer) as web,patch.object(b,'_kimi_deep') as plain:
            with model_router.task('knowledge'):self.assertEqual(b.deep_answer('latest question'),answer)
            web.assert_called_once();plain.assert_not_called()
    def test_excluded_price_does_not_force_search_but_current_policy_does(self):
        self.assertFalse(b.requires_current_sources('解释单一麦芽，不涉及价格'))
        self.assertTrue(b.requires_current_sources('不涉及价格，介绍今年招生政策'))

if __name__=='__main__':unittest.main()
