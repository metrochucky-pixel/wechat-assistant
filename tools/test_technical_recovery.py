import os,sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
_tmp=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_tmp.name+'/state.sqlite'
import wechat_autoreply as b
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
from PIL import Image

class Recovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.o=Outbox(self.tmp.name+'/db');self.p=TurnPlanner(self.o)
  self.o.add('朋友群',{'last_customer_msg':'图片','reply':'猜测','needs_image':True})
  self.job=self.o.next('朋友群')
  for name,value in [('OUTBOX',self.o),('TURNS',self.p)]:
   p=patch.object(b,name,value);p.start();self.addCleanup(p.stop)
 def test_two_failures_skip_without_owner_notification(self):
  with patch.object(b,'_ask_owner') as ask:
   b._technical_defer(self.job,'图移动')
   self.assertEqual(self.o.get(self.job['id'])['state'],'pending')
   b._technical_defer(self.o.get(self.job['id']),'仍不清楚')
   self.assertEqual(self.o.get(self.job['id'])['state'],'ignored');ask.assert_not_called()
 def test_owned_draft_is_recorded_for_cleanup_not_sent(self):
  self.job['payload']['_owned_draft']='旧草稿'
  b._technical_defer(self.job,'问题过时',abandon=True)
  j=self.o.get(self.job['id']);self.assertTrue(j['payload']['_cleanup_draft']);self.assertEqual(j['payload']['reply'],'')
 def test_unknown_send_is_never_retried(self):
  self.o.update(self.job['id'],'uncertain')
  b._technical_defer(self.job,'不明')
  self.assertEqual(self.o.get(self.job['id'])['state'],'uncertain')
 def cleanup(self,visible):
  d=self.job['payload'];d['_owned_draft']='旧草稿';d['_cleanup_draft']=True
  self.o.update(self.job['id'],'ignored',d)
  image=Image.new('RGB',(1080,2400),'white')
  with patch.object(b,'screencap',return_value=image),patch.object(b,'_verify_identity',return_value=True), \
       patch.object(b,'_inspect_chat',side_effect=[{'draft':visible},{'draft':'旧草稿'},{'draft':''}]), \
       patch.object(b,'_read_composer',side_effect=['旧草稿','']),patch.object(b,'_input_center',return_value=[500,2100]),patch.object(b,'set_ime'), \
       patch.object(b,'tap'),patch.object(b.time,'sleep'),patch.object(b,'adb') as adb:
   result=b._cleanup_bot_drafts('朋友群')
   return result,adb.call_args_list
 def test_owner_edited_draft_is_preserved(self):
  result,calls=self.cleanup('我改了内容');self.assertFalse(result);self.assertEqual(calls,[])
 def test_exact_bot_draft_cleared_and_verified(self):
  result,calls=self.cleanup('旧草稿');self.assertTrue(result)
  self.assertEqual(len(calls),1);self.assertIn('ADB_CLEAR_TEXT',calls[0].args)
  self.assertNotIn('_cleanup_draft',self.o.get(self.job['id'])['payload'])
 def test_verified_cleanup_does_not_reopen_members_after_focus(self):
  d=self.job['payload'];d['_owned_draft']='旧草稿';d['_cleanup_draft']=True
  self.o.update(self.job['id'],'ignored',d)
  image=Image.new('RGB',(1080,2400),'white');info={'is_chat':True,'title':'群聊(5)','draft':'旧草稿'}
  with patch.object(b,'screencap',return_value=image),patch.object(b,'_verify_identity') as verify, \
       patch.object(b,'_read_composer',side_effect=['旧草稿','']),patch.object(b,'_input_center',return_value=[500,2100]), \
       patch.object(b,'set_ime'),patch.object(b,'tap'),patch.object(b.time,'sleep'),patch.object(b,'adb'):
   self.assertTrue(b._cleanup_bot_drafts('朋友群',verified=(image,info)));verify.assert_not_called()
 def test_failed_cleanup_waits_instead_of_looping_models(self):
  self.cleanup('我改了内容')
  with patch.object(b,'_inspect_chat') as inspect:
   self.assertFalse(b._cleanup_bot_drafts('朋友群'));inspect.assert_not_called()

if __name__=='__main__':unittest.main()
