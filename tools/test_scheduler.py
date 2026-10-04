"""Offline scheduler regressions with real row pixels and isolated state."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from bot_scheduler import QuietRows,ListScanCache,priority,count
from bot_reliability import Outbox
_tmp=tempfile.TemporaryDirectory();os.environ['WECHATBOT_OUTBOX']=_tmp.name+'/import.sqlite'
import wechat_autoreply as b


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.now=100
        self.q=QuietRows(clock=lambda:self.now,ttl=60)
        self.c=ListScanCache(clock=lambda:self.now)
        self.img=Image.new('RGB',(1080,2400),'white')
        self.stamp=self.q.fingerprint(self.img,847)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.out=Outbox(self.tmp.name+'/db')

    def quiet(self):self.q.completed('朋友群',self.stamp,'skip_无需回复')

    def test_only_completed_no_reply_can_skip(self):
        for action in ('已发送','待答_观察不完整','发送未确认','待答_自动重读','待确认_已排队通知机主'):
            self.q.completed('朋友群',self.stamp,action)
            self.assertFalse(self.q.skip('朋友群',self.stamp))
        self.quiet();self.assertTrue(self.q.skip('朋友群',self.stamp))

    def test_new_preview_or_unread_badge_forces_visit(self):
        self.quiet()
        for xy in ((180,770),(300,850),(950,800)):
            changed=self.img.copy();changed.putpixel(xy,(255,0,0))
            self.assertFalse(self.q.skip('朋友群',self.q.fingerprint(changed,847)))

    def test_at_pending_or_unread_bypasses_silence(self):
        self.quiet();self.assertFalse(self.q.skip('朋友群',self.stamp,urgent=True))

    def test_active_job_prevents_marking_quiet(self):
        self.q.completed('朋友群',self.stamp,'skip_无需回复',pending=True)
        self.assertFalse(self.q.skip('朋友群',self.stamp))

    def test_skips_do_not_extend_periodic_recheck(self):
        self.quiet()
        for n in (110,125,159):
            self.now=n;self.assertTrue(self.q.skip('朋友群',self.stamp))
        self.now=160;self.assertFalse(self.q.skip('朋友群',self.stamp))

    def test_changed_failure_clears_prior_quiet_state(self):
        self.quiet();self.q.completed('朋友群',None,'exception')
        self.assertFalse(self.q.skip('朋友群',self.stamp))

    def test_row_move_uses_content_not_old_position(self):
        self.quiet();shifted=Image.new('RGB',self.img.size,'black')
        shifted.paste(self.img.crop((0,750,1080,944)),(0,944))
        self.assertTrue(self.q.skip('朋友群',self.q.fingerprint(shifted,1041)))

    def test_other_group_and_restart_need_inspection(self):
        self.quiet();self.assertFalse(self.q.skip('其他群',self.stamp))
        self.assertFalse(QuietRows().skip('朋友群',self.stamp))

    def test_list_cache_ignores_clock_but_not_changed_message(self):
        key=self.c.key(self.img,False,[]);self.c.put(key,[{'name':'朋友群'}])
        self.img.putpixel((50,30),(0,0,0));self.assertIsNotNone(self.c.get(self.c.key(self.img,False,[])))
        self.img.putpixel((500,850),(0,0,0));self.assertIsNone(self.c.get(self.c.key(self.img,False,[])))

    def test_list_target_and_patrol_changes_invalidate(self):
        key=self.c.key(self.img,False,['甲']);self.c.put(key,[])
        self.assertIsNone(self.c.get(self.c.key(self.img,False,['乙'])))
        self.assertIsNone(self.c.get(self.c.key(self.img,True,['甲'])))

    def test_list_cache_expires_and_copies(self):
        key=self.c.key(self.img,False,[]);self.c.put(key,[{'name':'朋友群'}])
        self.c.get(key).clear();self.assertEqual(len(self.c.get(key)),1)
        self.now=120;self.assertIsNone(self.c.get(key))

    def test_at_then_pending_then_fair_rotation(self):
        rows=[{'name':'idle'},{'name':'pending'},{'name':'mention','at_me':True},{'name':'older'}]
        rows.sort(key=lambda r:priority(r,{'pending'},{'idle':10,'older':1}))
        self.assertEqual([r['name'] for r in rows],['mention','pending','older','idle'])

    def test_model_scan_reuses_exact_image_and_refreshes_for_target(self):
        response={'is_list':True,'rows':[{'row_id':'R1','name':'朋友群','at_me':False}]}
        with patch.object(b,'OUTBOX',self.out),patch.object(b,'LIST_SCAN_CACHE',self.c),patch.object(b,'list_row_centers',return_value=[459]),patch.object(b,'_grok_vision_decide',return_value=response) as model:
            a=b.scan_list_with_llm(self.img);b.scan_list_with_llm(self.img)
            self.assertEqual(a[0]['y'],459);self.assertEqual(model.call_count,1)
            self.out.note_observation('另一个群');b.scan_list_with_llm(self.img)
            self.assertEqual(model.call_count,2)
        with self.out.connect() as db:
            self.assertEqual(db.execute("SELECT n FROM scheduler_counts WHERE kind='list_cache_hit'").fetchone()[0],1)

if __name__=='__main__':unittest.main()
