"""Regression tests: wrong image, missing coordinates, failed original, stale cache."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_state = tempfile.TemporaryDirectory()
os.environ['WECHATBOT_OUTBOX'] = _state.name + '/out.sqlite'
import wechat_autoreply as bot
import image_review as review
from PIL import Image


class ImageReview(unittest.TestCase):
    def setUp(self):
        from bot_reliability import Outbox
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        holder=patch.object(bot,'OUTBOX',Outbox(self.temp.name+'/test.sqlite'));holder.start();self.addCleanup(holder.stop)
        self.screen = Image.new('RGB', (1080, 2400), 'white')
        self.target = dict(unique=True, matches_request=True, sender='甲', kind='image',
                           bbox=[150, 1700, 650, 2100], description='酒标')
        self.check = dict(viewer=True, same_image=True, loading=False, original_available=False, needs_zoom=False)
        self.facts = dict(sufficient=True, observed='瓶身为蓝色', text='', uncertainty='品牌看不清')

    def run_review(self, responses):
        with ExitStack() as stack:
            for name in ('tap', 'back', 'adb'):
                stack.enter_context(patch.object(bot, name))
            stack.enter_context(patch.object(bot.time, 'sleep'))
            stack.enter_context(patch.object(bot, 'screencap', return_value=self.screen))
            stack.enter_context(patch.object(bot, '_compose_image_facts', return_value='这个蓝瓶挺特别'))
            read = stack.enter_context(patch.object(bot, '_guard_read', side_effect=responses[:1]+([{'crop_is_target_image':True}]+responses[1:] if len(responses)>1 else [])))
            result = bot.open_image_and_reply([540, 650], {'question':'看这张图', 'sender':'甲'}, evidence=self.screen)
            return result, read.call_count

    def test_new_bottom_image_selected_not_old_coordinate(self):
        result, calls = self.run_review([self.target, self.check, self.facts])
        self.assertEqual(result['bbox'], (150,1700,650,2100))
        self.assertEqual(calls, 4)
        self.assertIn('品牌看不清',result['facts']['uncertainty'])

    def test_multiple_candidates_fail_closed(self):
        result, calls = self.run_review([dict(self.target, unique=False)])
        self.assertIsNone(result); self.assertEqual(calls,1)

    def test_mis_scaled_box_rejected_by_crop_proof(self):
        read=Mock(side_effect=[self.target,{'crop_is_target_image':False}])
        self.assertIsNone(review.target(read,self.screen,{'sender':'甲'}))

    def test_pixel_candidates_include_newest_bottom_image(self):
        from PIL import ImageDraw
        draw=ImageDraw.Draw(self.screen)
        draw.rectangle((180,1650,500,2090),fill='navy')
        boxes,_=review.candidates(self.screen)
        self.assertTrue(any(r[1]>=1600 and r[3]>2050 for r in boxes))

    def test_wrong_sender_fail_closed(self):
        result, _ = self.run_review([dict(self.target,sender='乙')])
        self.assertIsNone(result)

    def test_wrong_enlarged_picture_never_composes(self):
        result, calls = self.run_review([self.target,dict(self.check,same_image=False)])
        self.assertIsNone(result); self.assertEqual(calls,3)

    def test_keyboard_change_is_not_a_reason_to_leave_chat(self):
        with patch.object(bot,'back') as back, patch.object(bot,'tap'), patch.object(bot.time,'sleep'), \
             patch.object(bot,'screencap',return_value=self.screen), \
             patch.object(bot,'_guard_read',side_effect=[self.target,{'crop_is_target_image':True},{'viewer':False}]):
            self.assertIsNone(bot.open_image_and_reply(None,{'sender':'甲'},evidence=self.screen))
            back.assert_not_called()

    def test_original_button_must_finish_loading(self):
        button=dict(self.check, original_available=True, original_label='查看原图 (3MB)', original_bbox=[400,2000,700,2070])
        loading=dict(self.check, loading=True)
        result, _ = self.run_review([self.target, button, loading, loading])
        self.assertIsNone(result)

    def test_download_original_and_read(self):
        button=dict(self.check, original_available=True, original_label='查看原图', original_bbox=[400,2000,700,2070])
        result, _ = self.run_review([self.target,button,self.check,self.facts])
        self.assertIsNotNone(result)

    def test_capture_returns_to_chat_before_background_facts(self):
        with patch.object(bot,'back') as back,patch.object(bot,'tap'),patch.object(bot.time,'sleep'), \
             patch.object(bot,'screencap',return_value=self.screen), \
             patch.object(bot,'_guard_read',side_effect=[self.target,{'crop_is_target_image':True},self.check]), \
             patch.object(review,'facts') as facts,patch.object(bot,'_compose_image_facts') as compose:
            result=bot.open_image_and_reply(None,{'sender':'甲'},evidence=self.screen,capture_only=True)
            self.assertEqual(len(result['frames']),1)
            self.assertTrue(result['metadata']['fingerprint'])
            facts.assert_not_called();compose.assert_not_called();back.assert_called_once()

    def test_unreadable_facts_cannot_compose(self):
        result, _ = self.run_review([self.target,self.check,dict(self.facts,sufficient=False)])
        self.assertIsNone(result)

    def test_unknown_loading_status_fails(self):
        result,_=self.run_review([self.target,dict(self.check,loading=None)])
        self.assertIsNone(result)

    def test_zoom_must_prove_detail_enlarged(self):
        result,_=self.run_review([self.target,dict(self.check,needs_zoom=True,detail_bbox=[200,600,700,1100]),
                                  dict(same_detail=True,detail_enlarged=False)])
        self.assertIsNone(result)

    def test_zoom_observed_before_composition(self):
        result,_=self.run_review([self.target,dict(self.check,needs_zoom=True,detail_bbox=[200,600,700,1100]),
                                  dict(same_detail=True,detail_enlarged=True),self.facts])
        self.assertTrue(result['zoomed'])

    def test_bounds_reject_avatar_composer_and_malformed(self):
        for rect in ([150,2200,650,2300], [150,0,650,100], [1,2,3,False], [100,200,20,50]):
            self.assertIsNone(review.target(lambda *a,**k:dict(self.target,bbox=rect),self.screen,{'sender':'甲'}))

    def test_cache_is_bound_to_revision_and_exact_reply(self):
        record=dict(version=1,revision=2,fingerprint='abc',facts={'observed':'蓝瓶'},reply='蓝瓶')
        payload=dict(_request_revision=2,_image_review=record,reply='蓝瓶')
        self.assertTrue(review.reviewed(payload))
        self.assertFalse(review.reviewed(dict(payload,_request_revision=3)))
        self.assertFalse(review.reviewed(dict(payload,reply='新答案')))
        self.assertFalse(review.reviewed({'_used_image':True}))

    def test_delivery_failure_never_sends_thumbnail_even_without_coordinates(self):
        for coord in (None,[540,650]):
            with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                path=folder+'/read.png';self.screen.save(path)
                payload=dict(needs_image=True,reply='缩略图猜的答案',_read_path=path,image_xy=coord)
                job=dict(id='test',payload=payload,display='朋友群',state='ready',due=0)
                stack.enter_context(patch.object(bot.TURNS,'valid',return_value=True))
                stack.enter_context(patch.object(bot,'_breaker_blocked',return_value=False))
                stack.enter_context(patch.object(bot,'is_mention_only_group',return_value=False))
                stack.enter_context(patch.object(bot.OUTBOX,'update'))
                stack.enter_context(patch.object(bot,'screencap',return_value=self.screen))
                stack.enter_context(patch.object(bot,'_turn_preflight',return_value=True))
                stack.enter_context(patch.object(bot,'open_image_and_reply',return_value=None))
                owner=stack.enter_context(patch.object(bot,'_technical_defer',return_value={'_action':'retry'}))
                send=stack.enter_context(patch.object(bot,'send_reply'))
                self.assertEqual(bot._deliver_job(job,set())['_action'],'retry')
                send.assert_not_called();owner.assert_called_once()
                self.assertEqual(payload['reply'],'')

    def test_unverified_fresh_reference_never_sends_thumbnail(self):
        job=dict(id='test',payload=dict(needs_image=True,reply='猜的'),display='朋友群',state='ready',due=0)
        with patch.object(bot.TURNS,'valid',return_value=True), patch.object(bot,'_breaker_blocked',return_value=False), \
             patch.object(bot.OUTBOX,'update'), patch.object(bot,'_technical_defer',return_value={'_action':'retry'}), \
             patch.object(bot,'screencap',return_value=self.screen), patch.object(bot,'_turn_preflight',return_value=False), \
             patch.object(bot,'send_reply') as send:
            self.assertEqual(bot._deliver_job(job,set())['_action'],'待答_看图前事项需刷新')
            send.assert_not_called()

if __name__=='__main__': unittest.main()
