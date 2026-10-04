import os,sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
_tmp=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_tmp.name+'/import.sqlite'
import wechat_autoreply as b
import bot_performance as perf
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
from PIL import Image

class RoundOne(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.o=Outbox(self.tmp.name+'/db');self.p=TurnPlanner(self.o)
  self.img=Image.new('RGB',(1080,2400),'white')
  self.snap=dict(is_chat=True,title='朋友群',at_latest=True,draft='',messages=[dict(sender='甲',text='今天终于放晴了',quote='',self=False,direct_mention=False,certain=True)])
  for name,value in [('OUTBOX',self.o),('TURNS',self.p)]:
   p=patch.object(b,name,value);p.start();self.addCleanup(p.stop)
  p=patch.dict(os.environ,{'WECHATBOT_CACHE_VISUAL':'1'});p.start();self.addCleanup(p.stop)
 def visit(self,fn):
  return perf.visit(lambda:self.o)(fn)(set(),'朋友群')
 def test_identity_and_messages_share_one_read(self):
  with patch.object(b,'_guard_read',return_value=self.snap) as model:
   def run(*args):
    self.assertEqual(b._inspect_chat(self.img)['title'],'朋友群')
    self.assertEqual(b._read_chat_frame(self.img)['messages'],self.snap['messages'])
    return {'_action':'skip_无需回复'}
   self.visit(run);self.assertEqual(model.call_count,1)
 def test_changed_message_or_composer_reloads_but_clock_does_not(self):
  with patch.object(b,'_guard_read',return_value=self.snap) as model:
   def run(*args):
    b._read_chat_frame(self.img)
    self.img.putpixel((50,30),(0,0,0));b._read_chat_frame(self.img)
    self.img.putpixel((50,900),(0,0,0));b._read_chat_frame(self.img)
    self.img.putpixel((50,2200),(0,0,0));b._read_chat_frame(self.img)
    return {'_action':'skip_无需回复'}
   self.visit(run);self.assertEqual(model.call_count,3)
 def test_cache_does_not_cross_visits_or_conversations(self):
  with patch.object(b,'_guard_read',return_value=self.snap) as model:
   def run(*args):b._read_chat_frame(self.img);return {}
   self.visit(run);self.visit(run);self.assertEqual(model.call_count,2)
 def test_cached_data_is_not_mutated_by_caller(self):
  def run(*args):
   perf.cached(self.img,'test',self.snap);a=perf.cached(self.img,'test');a['messages'].clear()
   self.assertEqual(len(perf.cached(self.img,'test')['messages']),1);return {}
  self.visit(run)
 def test_send_guard_is_fresh_even_on_identical_frame(self):
  with patch.object(b,'_guard_read',return_value=self.snap) as model:
   def run(*args):
    b._read_chat_frame(self.img)
    job={'payload':{'reply':'晴天真好','question_to_answer':'天气'}}
    b._inspect_chat(self.img,job);b._inspect_chat(self.img,job);return {}
   self.visit(run);self.assertEqual(model.call_count,3)
 def test_certified_bottom_reused_only_for_unchanged_pixels(self):
  with patch.object(b,'_refresh_latest',side_effect=lambda img:(img,True)) as scroll:
   def run(*args):
    b._latest_for_visit(self.img);b._latest_for_visit(self.img)
    self.img.putpixel((100,800),(0,0,0));b._latest_for_visit(self.img);return {}
   self.visit(run);self.assertEqual(scroll.call_count,2)
 def test_old_frame_expires(self):
  def run(*args):
   with patch.object(perf.time,'monotonic',return_value=100):perf.cached(self.img,'test',self.snap)
   with patch.object(perf.time,'monotonic',return_value=131):self.assertIsNone(perf.cached(self.img,'test'))
   return {}
  self.visit(run)
 def test_route_rejects_media_uncertainty_owner_and_knowledge(self):
  payload={'_request_kind':'chat','question_to_answer':'接话'}
  self.assertTrue(perf.simple_eligible(payload,self.snap))
  for changes in ({'needs_image':True},{'is_deep_q':True},{'_needs_owner':True},{'_request_kind':'vision'},{'question_to_answer':'查一下最新价格'},{'_owner_approved':True}):
   self.assertFalse(perf.simple_eligible(dict(payload,**changes),self.snap))
  for changes in ({'text':'[图片]'},{'certain':False},{'sender':''}):
   self.assertFalse(perf.simple_eligible(payload,dict(self.snap,messages=[dict(self.snap['messages'][0],**changes)])))
 def test_metrics_distinguish_failure_and_intentional_ignore(self):
  def run(*args):
   b._api_create('test',lambda **kw:'ok')
   perf.note('plan',[{'action':'ignore','reason':'已经回答'}]);return {'_action':'skip_无需回复'}
  self.visit(run)
  with self.o.connect() as db:r=db.execute('select * from performance_visits').fetchone()
  d=json.loads(r['payload']);self.assertEqual(d['api_calls'],1);self.assertEqual(r['outcome'],'no_reply_needed')
  self.assertEqual(perf.outcome('待答_观察不完整'),'observation_incomplete')
  self.assertEqual(perf.outcome('dry_run_旧草稿已清理'),'dry_run')
 def test_exception_also_records_and_resets_context(self):
  with self.assertRaises(ValueError):self.visit(lambda *args:(_ for _ in ()).throw(ValueError('test')))
  self.assertIsNone(perf._current.get())
  with self.o.connect() as db:self.assertEqual(db.execute('select outcome from performance_visits').fetchone()[0],'exception')
 def test_simple_draft_has_no_image_payload(self):
  response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"route":"simple","reply":"总算能出去走走了"}'))])
  with patch.object(b,'_api_create',return_value=response) as api,patch.object(b,'get_grok_client'),patch.object(b,'load_persona',return_value='自然说话'):
   d=b._simple_draft({'question_to_answer':'回应晴天'},self.snap,'朋友群')
   self.assertFalse(d['needs_image']);self.assertIsInstance(api.call_args.kwargs['messages'][1]['content'],str)
 def test_simple_draft_can_escalate_without_reply(self):
  response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"route":"complex","reply":"不能采用"}'))])
  with patch.object(b,'_api_create',return_value=response),patch.object(b,'get_grok_client'):
   self.assertIsNone(b._simple_draft({},self.snap,'朋友群'))

 def test_delivery_uses_text_route_but_does_not_send_in_dry_run(self):
  payload={'_request_kind':'chat','_needs_turn_draft':True,'question_to_answer':'回应天气','question_context':'大家说放晴了'}
  self.o.add('朋友群',payload);job=self.o.next('朋友群')
  with patch.object(b.TURNS,'valid',return_value=True),patch.object(b,'_breaker_blocked',return_value=False), \
       patch.object(b,'screencap',return_value=self.img),patch.object(b,'_turn_preflight',return_value=True), \
       patch.object(b,'_simple_draft',return_value={'reply':'总算放晴了','needs_image':False}) as draft, \
       patch.object(b,'read_and_decide') as full,patch.object(b,'send_reply') as send,patch.object(b,'DRY_RUN',True):
   def run(*args):
    perf.cached(self.img,'chat-frame-v1',self.snap)
    return b._deliver_job(job,set())
   result=self.visit(run)
   self.assertEqual(result['_action'],'dry_run_拟回复');draft.assert_called_once();full.assert_not_called();send.assert_not_called()
 def test_complex_escalation_retains_full_draft_path(self):
  payload={'_request_kind':'chat','_needs_turn_draft':True,'question_to_answer':'回应天气','question_context':'大家说放晴了'}
  self.o.add('朋友群',payload);job=self.o.next('朋友群')
  with patch.object(b.TURNS,'valid',return_value=True),patch.object(b,'_breaker_blocked',return_value=False), \
       patch.object(b,'screencap',return_value=self.img),patch.object(b,'_turn_preflight',return_value=True), \
       patch.object(b,'_simple_draft',return_value=None), \
       patch.object(b,'read_and_decide',return_value={'reply':'天气不错'}) as full,patch.object(b,'DRY_RUN',True):
   def run(*args):
    perf.cached(self.img,'chat-frame-v1',self.snap)
    return b._deliver_job(job,set())
   self.visit(run);full.assert_called_once()

if __name__=='__main__':unittest.main()
