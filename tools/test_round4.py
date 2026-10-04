"""Context isolation, original fact cache, receipt metrics and fresh-image recovery."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
import conversation_context as context
import image_fact_cache as cache
import reply_metrics
from PIL import Image
_state=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_state.name+'/import.sqlite'
import wechat_autoreply as bot


class Round4(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.out=Outbox(self.tmp.name+'/out.sqlite');self.planner=TurnPlanner(self.out)
        self.payload={'question_to_answer':'新加坡硕士专业申请条件','question_context':'不涉及费用，仅回答硕士','_request_sender':'甲','reply':'核对学历和语言要求'}
        self.im=Image.new('RGB',(100,200),'white')
    def remember(self,conv='群A',**changes):
        d=dict(self.payload,**changes);d['message_event']=str(time.time_ns());j=self.out.add(conv,d);self.out.update(j['id'],'sent',d);context.remember(self.out,j);return j
    def test_context_does_not_cross_conversations(self):
        self.remember('群AB');self.assertEqual(context.build(self.out,'群A',self.payload)['related_history'],[])
    def test_context_retains_complete_current_constraints(self):
        d=dict(self.payload,question_context='约束'*10000)
        self.assertEqual(context.build(self.out,'群A',d)['context'],d['question_context'])
    def test_unrelated_topic_excluded_and_sender_explicit(self):
        self.remember(_request_sender='乙');self.remember(question_to_answer='今天威士忌酒标是什么')
        data=context.build(self.out,'群A',self.payload)
        self.assertEqual(len(data['related_history']),1);self.assertEqual(data['related_history'][0]['sender'],'乙')
        self.assertFalse(data['related_history'][0]['same_requester'])
    def test_unconfirmed_turn_not_memory(self):
        j=self.out.add('群A',self.payload);context.remember(self.out,j)
        self.assertEqual(context.build(self.out,'群A',self.payload)['related_history'],[])
    def test_whole_history_items_bounded(self):
        self.remember(reply='长回复'*3000)
        self.assertFalse(context.build(self.out,'群A',self.payload)['related_history'])
    def test_context_discards_unrelated_speaker_message(self):
        messages=[{'sender':'乙','text':'周末跑步天气很好'},{'sender':'甲','text':'只回答硕士'}]
        data=context.build(self.out,'群A',self.payload,messages)
        self.assertEqual(data['messages'],messages[1:]);self.assertEqual(data['omitted_messages'],1)
    def test_original_fact_reuse_without_model_call(self):
        read=Mock(return_value={'sufficient':True,'observed':'蓝瓶','text':'','uncertainty':'品牌未知'})
        first=cache.analyze(self.out,read,[self.im],self.payload,scope='群A')
        second=cache.analyze(self.out,read,[self.im],self.payload,scope='群A')
        self.assertFalse(first[1]);self.assertTrue(second[1]);self.assertEqual(read.call_count,1)
    def test_question_group_zoom_or_image_changes_miss(self):
        k=cache.key([self.im],None,self.payload,'群A')
        changed=self.im.copy();changed.putpixel((20,80),(0,0,0))
        for frames,zoom,d,scope in [([changed],None,self.payload,'群A'),([self.im],self.im,self.payload,'群A'),([self.im],None,dict(self.payload,question_to_answer='年份是什么'),'群A'),([self.im],None,self.payload,'群B')]:
            self.assertNotEqual(k,cache.key(frames,zoom,d,scope))
    def test_clock_change_ignored_but_image_pixels_not(self):
        changed=self.im.copy();changed.putpixel((20,2),(0,0,0))
        self.assertEqual(cache.key([self.im],None,{},'A'),cache.key([changed],None,{},'A'))
    def test_insufficient_facts_not_cached(self):
        read=Mock(return_value={'sufficient':False})
        for _ in range(2):self.assertIsNone(cache.analyze(self.out,read,[self.im],{})[0])
        self.assertEqual(read.call_count,2)
    def test_expired_facts_reanalyzed(self):
        read=Mock(return_value={'sufficient':True,'observed':'蓝瓶','text':'','uncertainty':''})
        cache.analyze(self.out,read,[self.im],{})
        with self.out.connect() as db:db.execute('UPDATE image_fact_cache SET created=0')
        self.assertFalse(cache.analyze(self.out,read,[self.im],{})[1]);self.assertEqual(read.call_count,2)
    def test_metrics_only_receipts_not_generation_or_uncertain(self):
        j=self.out.add('群A',self.payload)
        for state in ('ready','sending','uncertain'):self.out.update(j['id'],state)
        with self.out.connect() as db:self.assertEqual(reply_metrics.report(db)['confirmed_samples'],0)
        self.out.update(j['id'],'sent');self.out.update(j['id'],'sent')
        with self.out.connect() as db:self.assertEqual(reply_metrics.report(db)['confirmed_samples'],1)
    def test_metrics_observation_to_receipt_includes_queue(self):
        from test_conversation_turns import msg,action
        now=time.time();p=TurnPlanner(self.out,clock=lambda:now-100)
        base=[msg('本人','已有回答',self_=True)];p.ingest('群A',base)
        ob=p.ingest('群A',base+[msg('甲','新加坡硕士申请条件')]);p.apply('群A',ob,[action('new',ob['events'])],False)
        p.clock=lambda:now;p.enqueue('群A',False);j=self.out.next('群A');self.assertIsNotNone(j)
        self.out.update(j['id'],'sent')
        with self.out.connect() as db:r=reply_metrics.report(db)
        self.assertGreaterEqual(r['max_seconds'],100);self.assertEqual(r['clock_basis_counts'],{'message_observed':1})
    def test_no_sample_is_unknown_not_zero(self):
        with self.out.connect() as db:r=reply_metrics.report(db)
        self.assertIsNone(r['discovery_to_receipt_p50_seconds']);self.assertIsNone(r['missed_reply_rate'])
    def test_image_missing_old_screenshot_relocates_after_preflight(self):
        j=self.out.add('群A',dict(self.payload,needs_image=True));j['due']=0
        with patch.object(bot,'OUTBOX',self.out),patch.object(bot.TURNS,'valid',return_value=True),patch.object(bot,'_breaker_blocked',return_value=False),patch.object(bot,'screencap',return_value=self.im),patch.object(bot,'_turn_preflight',return_value=True),patch.object(bot,'open_image_and_reply',return_value=None) as locate,patch.object(bot,'_technical_defer',return_value={'_action':'retry'}),patch.object(bot,'send_reply') as send:
            self.assertEqual(bot._deliver_job(j,set())['_action'],'retry');locate.assert_called_once();send.assert_not_called()
    def test_changed_request_prevents_relocation(self):
        j=self.out.add('群A',dict(self.payload,needs_image=True));j['due']=0
        with patch.object(bot,'OUTBOX',self.out),patch.object(bot.TURNS,'valid',return_value=True),patch.object(bot,'_breaker_blocked',return_value=False),patch.object(bot,'screencap',return_value=self.im),patch.object(bot,'_turn_preflight',return_value=False),patch.object(bot,'open_image_and_reply') as locate:
            self.assertIn('事项需刷新',bot._deliver_job(j,set())['_action']);locate.assert_not_called()

    def test_metric_failure_cannot_undo_confirmed_send(self):
        j=self.out.add('群A',self.payload)
        with patch.object(reply_metrics,'confirmed',side_effect=ValueError('bad telemetry')):
            self.out.update(j['id'],'sent')
        self.assertEqual(self.out.get(j['id'])['state'],'sent')
    def test_duplicate_event_distinct_jobs_reported(self):
        j=self.out.add('群A',self.payload);self.out.update(j['id'],'sent')
        with self.out.connect() as db:
            db.execute("INSERT INTO reply_receipts SELECT 'other',conv,event,discovered,confirmed,basis FROM reply_receipts")
            self.assertEqual(reply_metrics.report(db)['duplicate_event_receipts'],1)
    def test_background_cache_still_composes_new_reply(self):
        from background_worker import compute
        b=Mock();b.OUTBOX=self.out
        b._guard_read.return_value={'sufficient':True,'observed':'蓝瓶','text':'','uncertainty':'品牌未知'}
        b._compose_image_facts.side_effect=['回复一','回复二']
        request={'inputs':{'frames':[self.im],'request':{'question':'看看瓶子'},'metadata':{'fingerprint':'current'}},'payload':{},'display':'群A','phase':'image_facts'}
        first=compute(b,request);second=compute(b,request)
        self.assertEqual(b._guard_read.call_count,1);self.assertEqual(b._compose_image_facts.call_count,2)
        self.assertFalse(first['review']['cache_hit']);self.assertTrue(second['review']['cache_hit'])
        self.assertEqual(second['review']['reply'],'回复二')
    def test_history_same_requester_ranked_first(self):
        self.remember(_request_sender='甲');self.remember(_request_sender='乙')
        history=context.build(self.out,'群A',self.payload)['related_history']
        self.assertEqual([h['sender'] for h in history],['甲','乙'])


if __name__=='__main__':unittest.main()
