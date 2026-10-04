import sys,time,tempfile,unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
from test_conversation_turns import msg,action
class Recovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.out=Outbox(self.tmp.name+'/o.db');self.p=TurnPlanner(self.out);self.conv='朋友群'
  self.ob=self.p.ingest(self.conv,[msg('甲','周末出去打网球')]);self.stamp=datetime.now().strftime('%Y-%m-%d %H:%M')
 def test_recent_social_recovers_once_across_restart(self):
  ob=self.p.recover_social(self.conv,self.ob,self.stamp);self.assertTrue(ob.get('recovery_stamp'))
  a=action('new',ob['events'],qkind='chat');a['join_chat']=True;a['addressed_to_bot']=False
  self.p.apply(self.conv,ob,[a],False,allow_smalltalk=True)
  again=TurnPlanner(self.out).recover_social(self.conv,self.ob,self.stamp);self.assertFalse(again.get('recovery_stamp'))
 def test_no_old_or_unknown_timestamp(self):
  for stamp in ('','2020-01-01 12:00',None):self.assertFalse(self.p.recover_social(self.conv,self.ob,stamp).get('recovery_stamp'))
 def test_cannot_bypass_mentions_or_create_answer(self):
  ob=self.p.recover_social(self.conv,self.ob,self.stamp)
  with self.assertRaises(ValueError):self.p.apply(self.conv,ob,[action('new',ob['events'])],False,allow_smalltalk=True)
  a=action('new',ob['events'],qkind='chat');a['join_chat']=True
  with self.assertRaises(ValueError):self.p.apply(self.conv,ob,[a],True,allow_smalltalk=True)
 def test_no_recovery_from_self_uncertain_or_mention(self):
  for flags in ({'self':True},{'certain':False},{'sender':''},{'direct_mention':True}):
   ob=dict(self.ob,visible=[dict(self.ob['visible'][0],message=dict(self.ob['visible'][0]['message'],**flags))])
   self.assertFalse(self.p.recover_social(self.conv,ob,self.stamp).get('recovery_stamp'))
if __name__=='__main__':unittest.main()
