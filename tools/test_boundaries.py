import json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox,deadline,DeadlineExceeded
import owner_memory as m
import model_router as router
from bot_scheduler import find_pending_page
from PIL import Image

class Boundaries(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.out=Outbox(self.tmp.name+'/db');self.now=time.time()
 def sample(self,id='old',text='今天去跑步了',when=None):
  with self.out.connect() as db:
   m.init(db);db.execute('INSERT INTO owner_samples VALUES(?,?,?,?,?,?)',(id,'群',text,when or self.now-3600,self.now,'owner_likely'))
 def render(self):
  with m.natural_preferences.scope('群'):return m.render(self.out)
 def observation(self,text='刚刚我的新发言'):
  message=dict(self=True,certain=True,text=text,quote='',sender='本人')
  return dict(continuous=True,events=[dict(message,id='new')],visible=[dict(id='new',message=message)])
 def test_uncertain_does_not_revoke_unrelated_history(self):
  self.sample();j=self.out.add('群',dict(reply='机器人答复',last_customer_msg='问题'));self.out.update(j['id'],'uncertain')
  m.capture(self.out,'群',dict(continuous=True,events=[],visible=[]))
  self.assertEqual(m.status(self.out)['origins'],{'owner_likely':1})
 def test_same_word_before_bot_writing_is_preserved(self):
  self.sample(text='好的');j=self.out.add('群',dict(reply='好的',last_customer_msg='问题'));self.out.update(j['id'],'sent')
  m.capture(self.out,'群',dict(continuous=True,events=[],visible=[]));self.assertEqual(m.status(self.out)['origins'],{'owner_likely':1})
 def test_pending_origin_can_be_rechecked(self):
  j=self.out.add('群',dict(reply='机器人答复',last_customer_msg='问题'));self.out.update(j['id'],'uncertain')
  m.capture(self.out,'群',self.observation());self.assertEqual(m.status(self.out)['origins'],{'pending_origin':1})
  self.out.update(j['id'],'sent')
  with patch.object(m.time,'time',return_value=self.now+15):m.capture(self.out,'群',self.observation())
  self.assertEqual(m.status(self.out)['origins'],{'owner_likely':1})
 def test_word_addition_and_source_revocation(self):
  m.set_preference(self.out,'avoid_words',['排面','排长'],'owner_explicit','owner')
  self.sample('one','bot 别再说老铁',self.now);self.sample('two','bot 别再说绝绝子',self.now+1);m.summarize(self.out)
  self.assertTrue(all(w in self.render() for w in ['排面','排长','老铁','绝绝子']))
  with self.out.connect() as db:db.execute("UPDATE owner_samples SET origin='excluded' WHERE event_id='one'")
  m.summarize(self.out);self.assertNotIn('老铁',self.render());self.assertIn('绝绝子',self.render());self.assertIn('排面',self.render())
 def test_duplicate_word_retains_other_source(self):
  self.sample('one','bot 别再说老铁',self.now);self.sample('two','bot 别再说老铁',self.now+1);m.summarize(self.out)
  with self.out.connect() as db:db.execute("UPDATE owner_samples SET origin='excluded' WHERE event_id='one'")
  m.summarize(self.out);self.assertIn('老铁',self.render());self.assertEqual(len(m.status(self.out)['word_evidence']),1)
 def test_manual_disable_blocks_word_relearning(self):
  m.set_preference(self.out,'avoid_words',['排面'],'owner_explicit','owner')
  with self.out.connect() as db:db.execute("UPDATE owner_preferences SET active=0 WHERE key='avoid_words'")
  self.sample('one','bot 别再说老铁',self.now);m.summarize(self.out);self.assertEqual(self.render(),'')
 def test_delivery_receives_reserved_calls(self):
  with router.task('visit'):
   for _ in range(5):router.timeout()
   router.begin_delivery()
   for _ in range(6):router.timeout()
   self.assertEqual(router._task.get()['calls'],6)
 def test_delivery_transition_cannot_refill_budget(self):
  with router.task('visit'):
   router.begin_delivery()
   for _ in range(12):router.timeout()
   router.begin_delivery()
   with self.assertRaises(router.BudgetExceeded):router.timeout()
 def test_nested_read_does_not_refill_delivery_budget(self):
  with router.task('visit'):
   router.begin_delivery()
   with router.task('read'):router.timeout()
   self.assertEqual(router._task.get()['kind'],'delivery');self.assertEqual(router._task.get()['calls'],1)
 def test_delivery_cannot_extend_enclosing_deadline(self):
  with self.assertRaises(DeadlineExceeded):
   with deadline(.03):
    with router.task('visit'):
     router.begin_delivery();time.sleep(.1)
 def test_short_delivery_deadline_stops(self):
  with patch.dict(router.PROFILES,{'delivery':dict(total=.02,call=.01,attempts=2)}):
   with self.assertRaises(DeadlineExceeded):
    with router.task('visit'):router.begin_delivery();time.sleep(.1)
 def test_discovered_mention_durable_and_prioritized(self):
  first=Image.new('RGB',(20,40),'white');last=Image.new('RGB',(20,40),'blue');shots=iter([first,last]);rows=[dict(name='目标群',at_me=False,y=600),dict(name='新群',at_me=True,y=300)]
  result=find_pending_page(first,['目标群'],lambda:next(shots),lambda im:rows,lambda im:None,lambda a,b:True,on_mention=lambda r:self.out.note_mention(r['name']))
  self.assertEqual([r['name'] for r in result[1]],['新群','目标群']);self.assertTrue(self.out.has_mention('新群'))
 def test_mention_interrupts_search_without_target(self):
  first=Image.new('RGB',(20,40),'white');last=Image.new('RGB',(20,40),'blue');shots=iter([first,last]);saved=[]
  result=find_pending_page(first,['目标群'],lambda:next(shots),lambda im:[dict(name='新群',at_me=True,y=300)],lambda im:None,lambda a,b:True,on_mention=saved.append)
  self.assertEqual(len(saved),1);self.assertEqual(result[1][0]['name'],'新群')

 def test_real_send_entry_uses_reserved_budget_for_all_checks(self):
  import os
  from contextlib import ExitStack
  os.environ['WECHATBOT_OUTBOX']=self.tmp.name+'/import.sqlite'
  import wechat_autoreply as b
  j=self.out.add('群',dict(reply='已核对的回复',last_customer_msg='问题'))
  frame=Image.new('RGB',(100,200),'white');calls=[0]
  def inspect(*args):
   router.timeout();calls[0]+=1
   return dict(is_chat=True,title='群',draft='' if calls[0]==1 else '已核对的回复')
  def check(*args):router.timeout();return True
  with ExitStack() as stack:
   for name,value in [('OUTBOX',self.out),('screencap',lambda:frame),('_inspect_chat',inspect),('_verify_identity',check),('_turn_preflight',check),('_receipt',check),('_pace_send',lambda:None),('_save_before',lambda *a:None),('_input_center',lambda im:[20,180]),('_stable_screen',lambda *a,**kw:True),('set_ime',lambda *a:None),('type_chinese',lambda *a:None),('find_send_button',lambda im:[90,180]),('_turn_commit',lambda j:True)]:
    stack.enter_context(patch.object(b,name,value))
   tap=stack.enter_context(patch.object(b,'tap'));stack.enter_context(patch.object(b.time,'sleep'))
   with router.task('visit'):
    for _ in range(5):router.timeout()
    self.assertTrue(b.send_reply('已核对的回复',j))
    self.assertEqual(router._task.get()['calls'],7)
   self.assertEqual(tap.call_args.args,(90,180))

if __name__=='__main__':unittest.main()
