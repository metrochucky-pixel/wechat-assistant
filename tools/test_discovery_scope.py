"""Discovery loss, certified empty pages, style scope and adaptive polling regressions."""
import os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from bot_reliability import Outbox
import bot_scheduler as s
import owner_memory as m
import natural_preferences as n
_import=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_import.name+'/db'
import wechat_autoreply as b


class DiscoveryScope(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.out=Outbox(self.tmp.name+'/db');self.clock=1000
        self.frame=Image.new('RGB',(1080,2400),'white')
    def sample(self,text,conv='群a',eid='e'):
        with self.out.connect() as db:
            m.init(db)
            db.execute('INSERT INTO owner_samples VALUES(?,?,?,?,?,?)',
                       (eid,conv,text,time.time(),time.time(),'owner_likely'))
    def render(self,conv):
        with n.scope(conv):return m.render(self.out)
    def scan(self,response):
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'LIST_SCAN_CACHE',s.ListScanCache()), \
             patch.object(b,'list_row_centers',return_value=[459]),patch.object(b,'_grok_vision_decide',return_value=response):
            return b.scan_list_with_llm(self.frame)
    def test_normal_unread_stops_search_and_survives_restart(self):
        later=Image.new('RGB',self.frame.size,'blue');shots=iter([self.frame,later])
        rows=[dict(name='普通未读群',unread=True,at_me=False,y=459)]
        with patch.object(b,'OUTBOX',self.out):
            result=s.find_pending_page(self.frame,['待答群'],lambda:next(shots),lambda im:rows,
                lambda im:None,lambda a,b:True,on_discovery=b._retain_list_discovery)
        self.assertIs(result[0],later);self.assertEqual(result[1],rows)
        self.assertIn('普通未读群',Outbox(self.out.path).targets())
    def test_initial_page_retention_does_not_store_click_coordinates(self):
        with patch.object(b,'OUTBOX',self.out):
            b._retain_list_discovery(dict(name='第一页未读群',unread=True,y=459))
        self.assertIn('第一页未读群',self.out.targets())
        with self.out.connect() as db:
            self.assertEqual(set(db.execute('SELECT * FROM observations').fetchone().keys()),{'conv','display','due'})
    def test_mention_priority_and_both_discoveries_retained(self):
        rows=[dict(name='普通',unread=True,at_me=False),dict(name='提及',at_me=True)]
        with patch.object(b,'OUTBOX',self.out):
            result=s.find_pending_page(self.frame,['目标'],lambda:self.frame,lambda im:rows,
                lambda im:None,lambda a,b:True,on_discovery=b._retain_list_discovery)
        self.assertEqual(result[1][0]['name'],'提及');self.assertTrue(self.out.has_mention('提及'))
        self.assertIn('普通',self.out.targets())
    def test_patrol_only_and_excluded_rows_not_retained(self):
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'is_no_reply',side_effect=lambda name:name=='排除群'):
            for r in [dict(name='安静群'),dict(name='文件传输助手',at_me=True),dict(name='排除群',unread=True)]:
                b._retain_list_discovery(r)
        self.assertEqual(self.out.targets(),[])
    def test_unread_is_scheduled_ahead_of_older_quiet_rows(self):
        rows=[dict(name='安静'),dict(name='未读',unread=True),dict(name='提及',at_me=True)]
        rows.sort(key=lambda row:s.priority(row,set(),{'未读':99}))
        self.assertEqual([r['name'] for r in rows],['提及','未读','安静'])
    def test_certified_empty_list_can_search(self):
        rows=self.scan(dict(is_list=True,rows=[]));self.assertFalse(rows)
        self.assertTrue(s.verified_list(rows,self.frame))
        patrol=s.OffscreenPatrol(clock=lambda:self.clock)
        self.assertEqual(patrol.choose(['屏外待答群'],rows),[]);self.clock+=31
        self.assertEqual(patrol.choose(['屏外待答群'],rows),['屏外待答群'])
    def test_wrong_page_and_invalid_rows_cannot_authorize_search(self):
        for response in [dict(is_list=False,rows=[]),dict(is_list=True,rows=[dict(row_id='missing',name='x',at_me=False)])]:
            self.assertFalse(s.verified_list(self.scan(response),self.frame))
        self.assertFalse(s.verified_list([],self.frame))
    def test_certification_is_bound_to_frame_and_survives_cache(self):
        rows=self.scan(dict(is_list=True,rows=[]));cache=s.ListScanCache()
        key=cache.key(self.frame,False,[]);cache.put(key,rows)
        self.assertTrue(s.verified_list(cache.get(key),self.frame))
        changed=self.frame.copy();changed.putpixel((500,500),(0,0,0))
        self.assertFalse(s.verified_list(rows,changed))
    def test_scanner_preserves_model_unread_indicator(self):
        rows=self.scan(dict(is_list=True,rows=[dict(row_id='R1',name='测试群',at_me=False,unread=True)]))
        self.assertTrue(rows[0]['unread']);self.assertTrue(s.verified_list(rows,self.frame))
    def test_legacy_emoji_phrase_only_affects_its_group(self):
        self.sample('机器人不要表情');m.summarize(self.out)
        self.assertIn('少用表情',self.render('群a'));self.assertEqual(self.render('群b'),'')
        self.assertEqual(m.render(self.out),'')
    def test_existing_global_observed_rule_is_suppressed_before_refresh(self):
        with self.out.connect() as db:
            m.init(db);db.execute("INSERT INTO owner_preferences VALUES('emoji','\"less\"','observed_correction','old',?,1)",(time.time(),))
        self.assertEqual(self.render('群b'),'')
        m.summarize(self.out);self.assertEqual(self.render('群b'),'')
    def test_unsupported_scalar_never_falls_back_to_global(self):
        self.sample('机器人不要表情')
        with patch.object(n,'extract',return_value=[]):m.summarize(self.out)
        self.assertEqual(self.render('群a'),'');self.assertEqual(self.render('群b'),'')
    def test_observed_words_scoped_manual_words_global_and_revocation_immediate(self):
        m.set_preference(self.out,'avoid_words',['排长'],'owner_explicit','owner')
        self.sample('bot 别再说老铁');m.summarize(self.out)
        self.assertIn('老铁',self.render('群a'));self.assertNotIn('老铁',self.render('群b'))
        self.assertIn('排长',self.render('群b'))
        with self.out.connect() as db:db.execute("UPDATE owner_samples SET origin='excluded'")
        self.assertNotIn('老铁',self.render('群a'))
    def test_unchanged_group_can_skip_after_slow_round(self):
        q=s.QuietRows(clock=lambda:self.clock);q.completed('群','stamp','skip_无需回复')
        self.clock+=70;self.assertTrue(q.skip('群','stamp'))
        self.clock+=50;self.assertFalse(q.skip('群','stamp'))
    def test_verified_unchanged_checks_backoff_with_hard_cap(self):
        q=s.QuietRows(clock=lambda:self.clock)
        for interval in [120,240,300,300]:
            q.completed('群','stamp','skip_无需回复')
            self.clock+=interval-1;self.assertTrue(q.skip('群','stamp'))
            self.clock+=1;self.assertFalse(q.skip('群','stamp'))
    def test_new_content_or_urgency_resets_backoff(self):
        for urgent,stamp in [(True,'old'),(False,'changed')]:
            q=s.QuietRows(clock=lambda:self.clock)
            q.completed('群','old','skip_无需回复');q.completed('群','old','skip_无需回复')
            self.assertFalse(q.skip('群',stamp,urgent=urgent))
            q.completed('群',stamp,'skip_无需回复');self.clock+=120
            self.assertFalse(q.skip('群',stamp))


if __name__=='__main__':unittest.main()
