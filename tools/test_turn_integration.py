"""Exercise production observation/preflight with deterministic model JSON, isolated state."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_state = tempfile.TemporaryDirectory()
os.environ['WECHATBOT_CACHE_VISUAL']='0'
os.environ['WECHATBOT_OUTBOX'] = _state.name+'/import.sqlite'
import wechat_autoreply as bot
from bot_reliability import Outbox
from conversation_planner import TurnPlanner
from test_conversation_turns import msg, action
from PIL import Image

class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.out=Outbox(self.tmp.name+'/out.sqlite'); self.now=1000
        self.p=TurnPlanner(self.out,clock=lambda:self.now)
        self.img=Image.new('RGB',(100,100),'white'); self.base=[msg('本人','历史回答',self_=True)]
        for name,value in [('OUTBOX',self.out),('TURNS',self.p),('_refresh_latest',lambda img:(img,False))]:
            p=patch.object(bot,name,value); p.start(); self.addCleanup(p.stop)

    def observation(self, messages, title='MentionOnly'):
        return dict(title=title,at_latest=True,messages=messages)

    def model(self, images, prompt, **kw):
        if '数据：' not in prompt: return self.observation(self.base)
        import json
        data=json.JSONDecoder().raw_decode(prompt.split('数据：')[-1])[0]
        es=data['new_events']
        acts=[action('ignore',[e]) if e['self'] else action('new',[e],e['sender'],e['text']) for e in es]
        return {'actions':acts}

    def test_observation_to_request_to_job_without_replaying_baseline(self):
        with patch.object(bot,'_guard_read',side_effect=self.model):
            self.assertFalse(bot._observe_turns('MentionOnly',self.img))
            self.base.append(msg('甲','@Bot Example 画只猫',True))
            self.assertTrue(bot._observe_turns('MentionOnly',self.img))
            self.p.enqueue('MentionOnly',True); self.assertIsNone(self.out.next('MentionOnly'))
            self.now+=7; self.p.enqueue('MentionOnly',True)
            job=self.out.next('MentionOnly'); self.assertIsNotNone(job)
            self.assertTrue(bot._turn_preflight(job,self.img))
            self.assertTrue(bot._turn_commit(job)); self.assertEqual(self.out.get(job['id'])['state'],'sending')

    def test_model_failure_preserves_events_for_retry(self):
        self.p.ingest('MentionOnly',self.base); self.base.append(msg('甲','@Bot Example 问题',True))
        with patch.object(bot,'_guard_read',side_effect=lambda images,prompt,**k:self.observation(self.base) if '数据：' not in prompt else {}):
            with self.assertRaises(ValueError):bot._observe_turns('MentionOnly',self.img)
        with patch.object(bot,'_guard_read',side_effect=self.model):
            self.assertTrue(bot._observe_turns('MentionOnly',self.img))
        self.assertEqual(len(self.p.requests('MentionOnly')),1)

    def test_preflight_correction_prevents_commit(self):
        self.p.ingest('MentionOnly',self.base); self.base.append(msg('甲','@Bot Example 本科',True))
        with patch.object(bot,'_guard_read',side_effect=self.model):bot._observe_turns('MentionOnly',self.img)
        r=self.p.requests('MentionOnly')[0]; self.now+=7; self.p.enqueue('MentionOnly',True); j=self.out.next('MentionOnly')
        self.base.append(msg('甲','改硕士'))
        def update(images,prompt,**kw):
            if '数据：' not in prompt:return self.observation(self.base)
            import json
            data=json.JSONDecoder().raw_decode(prompt.split('数据：')[-1])[0]; return {'actions':[action('update',data['new_events'],question='硕士',rid=r['id'])]}
        with patch.object(bot,'_guard_read',side_effect=update):self.assertFalse(bot._turn_preflight(j,self.img))
        self.assertFalse(bot._turn_commit(j)); self.assertEqual(self.out.get(j['id'])['state'],'ignored')

    def test_reading_another_conversation_cannot_create_requests(self):
        with patch.object(bot,'_guard_read',return_value=self.observation(self.base,'另一个群')):
            self.assertFalse(bot._observe_turns('MentionOnly',self.img))
        self.assertEqual(self.p.requests('MentionOnly'),[])

    def test_gap_invalidates_existing_question_even_on_next_stable_screen(self):
        with patch.object(bot,'_guard_read',side_effect=self.model):
            bot._observe_turns('MentionOnly',self.img); self.base.append(msg('甲','@Bot Example 问题',True));bot._observe_turns('MentionOnly',self.img)
            self.now+=7;self.p.enqueue('MentionOnly',True); j=self.out.next('MentionOnly')
            self.base=[msg('乙','另外一屏')]
            self.assertFalse(bot._turn_preflight(j,self.img))
            self.assertFalse(bot._turn_preflight(j,self.img))
            self.assertFalse(bot._turn_commit(j))

    def test_identical_pixels_reuse_extraction_but_changed_pixels_reread(self):
        with patch.dict(os.environ, {'WECHATBOT_CACHE_VISUAL':'1'}):
            with patch.object(bot,'_guard_read',side_effect=self.model) as model:
                self.assertFalse(bot._observe_turns('MentionOnly',self.img))
                self.assertTrue(bot._observe_turns('MentionOnly',self.img))
                self.assertEqual(model.call_count,1)
                self.img.putpixel((50,50),(255,0,0))
                self.assertTrue(bot._observe_turns('MentionOnly',self.img))
                self.assertEqual(model.call_count,2)

    def test_narrow_bottom_check_recovers_only_with_matching_positive_evidence(self):
        extract=self.observation(self.base);extract['at_latest']=False
        bottom=dict(composer_visible=True,bottom_message_complete=True,new_message_hint=False)
        with patch.object(bot,'_guard_read',side_effect=[extract,bottom]):
            self.assertFalse(bot._observe_turns('MentionOnly',self.img))  # Stored baseline, no historical reply.
        with self.out.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM turn_events').fetchone()[0],1)
        for changes in ({'composer_visible':False},{'new_message_hint':True},{'bottom_message_complete':False}):
            check=dict(bottom,**changes)
            extract['at_latest']=False
            with patch.object(bot,'_guard_read',side_effect=[extract,check]):
                self.assertFalse(bot._observe_turns('MentionOnly',self.img))

    def test_owned_composer_is_not_a_new_outgoing_message(self):
        self.p.ingest('MentionOnly',self.base)
        draft='这玩偶太抢镜了'
        extracted=self.observation(self.base+[msg('__self__',draft+'[捂脸]',self_=True)])
        with patch.object(bot,'_guard_read',return_value=extracted) as model:
            self.assertTrue(bot._observe_turns('MentionOnly',self.img,composer_text=draft))
            self.assertEqual(model.call_count,1)
        with self.out.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM turn_events').fetchone()[0],1)

    def test_false_mention_of_other_person_rejected(self):
        self.p.ingest('MentionOnly',self.base); self.base.append(msg('甲','@别人 问题',True))
        with patch.object(bot,'_guard_read',side_effect=self.model):
            with self.assertRaises(ValueError):bot._observe_turns('MentionOnly',self.img)
        self.assertEqual(self.p.requests('MentionOnly'),[])

class SchedulingAndProviders(unittest.TestCase):
    setUp = Integration.setUp
    def test_discovery_alias_does_not_relax_identity_check(self):
        with patch.dict(os.environ,{'WECHAT_DISCOVERY_ALIASES':'{"兴趣群误识别04":"示例兴趣群04"}'}):
            self.assertEqual(bot._discovered_name('兴趣群误识别04'),'示例兴趣群04')
            self.assertEqual(bot._discovered_name('示例兴趣群03'),'示例兴趣群03')
            self.assertFalse(bot._verify_identity(self.img,'示例兴趣群04',{'is_chat':True,'title':'兴趣群误识别04'}))

    def test_settle_waits_then_reads_correction_before_enqueue(self):
        with patch.object(bot.time, 'time', return_value=1000), \
             patch.object(bot.time, 'sleep') as sleep, \
             patch.object(bot.TURNS, 'requests', return_value=[{'eligible':1006}]), \
             patch.object(bot, 'screencap', return_value=self.img), \
             patch.object(bot, '_observe_turns', return_value=True) as observe:
            bot._settle_turns('朋友群')
        sleep.assert_called_once_with(6.05)
        observe.assert_called_once_with('朋友群', self.img, bottom_verified=False)

    def test_settle_does_not_hide_failed_revalidation(self):
        with patch.object(bot.time, 'time', return_value=1000), \
             patch.object(bot.time, 'sleep'), \
             patch.object(bot.TURNS, 'requests', return_value=[{'eligible':1006}]), \
             patch.object(bot, 'screencap', return_value=self.img), \
             patch.object(bot, '_observe_turns', return_value=False):
            with self.assertRaises(ValueError):bot._settle_turns('朋友群')

    def test_provider_timeout_and_cooldown_then_recovery(self):
        from unittest.mock import Mock
        fail=Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):bot._api_create('test',fail,timeout=90)
        self.assertEqual(fail.call_args.kwargs['timeout'],20)
        with self.assertRaises(RuntimeError):bot._api_create('test',fail)
        self.assertEqual(fail.call_count,1)
        with self.out.connect() as db:db.execute("UPDATE providers SET until=0 WHERE name='test'")
        self.assertEqual(bot._api_create('test',lambda **kw:'ok'),'ok')
        with self.out.connect() as db:
            self.assertIsNone(db.execute("SELECT * FROM providers WHERE name='test'").fetchone())

if __name__=='__main__':unittest.main()
