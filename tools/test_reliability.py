"""Offline failure-path regressions; all device, API and notification calls are mocked."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
import time
from unittest.mock import patch, Mock
from types import SimpleNamespace
from PIL import Image
import os
import atexit
import tempfile
_import_state = tempfile.TemporaryDirectory(prefix='wechatbot-test-')
atexit.register(_import_state.cleanup)
os.environ['WECHATBOT_CACHE_VISUAL']='0'
os.environ['WECHATBOT_OUTBOX'] = str(Path(_import_state.name) / 'import.sqlite3')
import wechat_autoreply as bot
from bot_reliability import Outbox, bounded, same_conversation, validate_decision
from conversation_planner import TurnPlanner


class Reliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.q = Outbox(str(Path(self.tmp.name) / 'test.sqlite3'))
        self.img = Image.new('RGB', (1080,2400), 'white')
        self.patches = []
        def stub(name, value):
            p = patch.object(bot,name,value); p.start(); self.addCleanup(p.stop)
        self.stub = stub
        stub('HERE',self.tmp.name); stub('OUTBOX',self.q); stub('DRY_RUN',False)
        for name in ('tap','back','type_chinese','set_ime','_pace_send','log_decision','set_pointer',
                     'append_memory','add_todo','close_todo','_breaker_on_ok','_breaker_on_fail'):
            stub(name,Mock())
        stub('screencap',Mock(return_value=self.img)); stub('_inspect_chat',Mock(return_value={'is_chat':True,'title':'朋友群','draft':'','input_xy':[500,2220]}))
        stub('_verify_identity',Mock(return_value=True)); stub('convert_voices',Mock(return_value=(False,self.img)))
        for name in ('get_memory','get_pointer','get_gags','get_todos'):
            stub(name,Mock(return_value=''))
        for name in ('is_no_reply','is_official_name','is_fun_group','_breaker_blocked','leaks_internal'):
            stub(name,Mock(return_value=False))
        stub('notify_feishu',Mock(return_value=True)); stub('send_reply',Mock(return_value=False))
        stub('fingerprint',lambda conv,msg,img: conv+':'+msg)
        self.d={'conversation':'朋友群','last_customer_msg':'你好朋友','last_from_customer':True,'need_reply':True,'reply':'你好呀'}
        stub('read_and_decide',Mock(side_effect=lambda *a,**kw: dict(self.d)))
        self.seen=set()
        planner = TurnPlanner(self.q, clock=lambda: time.time()-20)
        stub('TURNS', planner)
        # Delivery tests use a deterministic observed-event fixture. Extraction/semantic planning
        # are covered separately by test_turn_integration and test_conversation_turns.
        observed = set()
        def observe(conv, img, **kwargs):
            if self.d.get('_skip_seen') or self.d.get('conversation') != conv:
                return False
            if conv in observed:
                return True
            observed.add(conv)
            mention = self.d.get('new_direct_mention') is True
            texts = [self.d.get('question_to_answer') or self.d['last_customer_msg']]
            texts += [x['question'] for x in self.d.get('pending_questions', [])]
            base = dict(sender='自己', text='历史已答', quote='', self=True, direct_mention=False, certain=True)
            planner.ingest(conv, [base])
            messages = [dict(sender='提问者'+str(i), text=('@Bot Example ' if mention else '')+q,
                        quote='', self=False, direct_mention=mention, certain=True) for i,q in enumerate(texts)]
            ob = planner.ingest(conv, [base]+messages)
            actions = [dict(action='new',event_ids=[e['id']],sender=e['sender'],addressed_to_bot=True,
                        question=texts[i],context=self.d.get('question_context',''),kind='image' if self.d.get('wants_image') else 'answer',
                        image_prompt=self.d.get('image_prompt',''),reason='test fixture') for i,e in enumerate(ob['events'])]
            planner.apply(conv,ob,actions,bot.is_mention_only_group(conv)); planner.clock=time.time; planner.enqueue(conv,bot.is_mention_only_group(conv))
            with self.q.connect() as db: rows=db.execute('SELECT id FROM jobs').fetchall()
            for row in rows:
                j=self.q.get(row['id']); payload=j['payload']
                if not payload.get('_request_id'): continue
                payload.update({k:v for k,v in self.d.items() if k not in ('question_to_answer','question_context','last_customer_msg','pending_questions')})
                payload['is_deep_q']=self.d.get('is_deep_q',False)
                payload['_fingerprint']=conv+':fixture'
                self.q.update(j['id'],'pending',payload)
            return True
        stub('_observe_turns', Mock(side_effect=observe))
        stub('_refresh_latest', lambda img:(img,False))
        stub('_turn_preflight', Mock(return_value=True))
        def commit(job):
            self.q.update(job['id'],'sending',job['payload']); return True
        stub('_turn_commit', Mock(side_effect=commit))

    def run_conversation(self, name='朋友群', at=False):
        return bot.process_one_conversation(self.seen,name,at)

    def test_initial_identity_failure_has_no_read_send_or_commit(self):
        bot._verify_identity.return_value=False
        self.run_conversation()
        bot.read_and_decide.assert_not_called(); bot.send_reply.assert_not_called(); bot.set_pointer.assert_not_called()

    def test_model_title_mismatch_stops_send(self):
        self.d['conversation']='另一个完全不同群'
        self.run_conversation()
        bot.send_reply.assert_not_called(); bot.set_pointer.assert_not_called(); 
        with self.q.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)

    def test_failed_send_preserves_pending_and_pointer(self):
        result=self.run_conversation()
        self.assertEqual(result['_action'],'发送失败'); self.assertTrue(self.q.active('朋友群'))
        bot.set_pointer.assert_not_called(); self.assertFalse(self.seen)
        reopened=Outbox(self.q.path)
        self.assertTrue(reopened.active('朋友群'))

    def test_cooldown_does_not_mark_seen(self):
        bot._breaker_blocked.return_value=True
        self.run_conversation()
        bot.send_reply.assert_not_called(); bot.set_pointer.assert_not_called(); self.assertFalse(self.seen)
        self.assertTrue(self.q.active('朋友群'))

    def test_confirmed_send_commits(self):
        bot.send_reply.return_value=True
        self.run_conversation()
        self.assertFalse(self.q.active('朋友群')); self.assertTrue(self.seen)
        bot.set_pointer.assert_called_once()

    def test_trade_no_at_no_reply(self):
        self.run_conversation('MentionOnly')
        bot.send_reply.assert_not_called(); bot.read_and_decide.assert_not_called()

    def test_trade_mention_survives_badge_loss_and_restart(self):
        self.d.update(conversation='MentionOnly',new_direct_mention=True)
        self.run_conversation('MentionOnly',True)
        self.assertTrue(self.q.active('MentionOnly'))  # Per-request authorization persists.
        with self.q.connect() as db: db.execute('UPDATE jobs SET due=0')
        self.stub('OUTBOX',Outbox(self.q.path)); bot.send_reply.return_value=True
        self.run_conversation('MentionOnly',False)
        self.assertEqual(bot.send_reply.call_count,2)
        self.assertFalse(self.q.has_mention('MentionOnly')); self.assertFalse(self.q.active('MentionOnly'))

    def test_list_at_does_not_authorize_ordinary_chatter(self):
        self.d.update(conversation='MentionOnly',new_direct_mention=False)
        self.run_conversation('MentionOnly',True)
        bot.send_reply.assert_not_called(); bot.set_pointer.assert_not_called()

    def test_image_failure_not_complete_and_no_untracked_caption(self):
        self.d.update(wants_image=True,image_prompt='小猫')
        self.stub('gen_image',Mock(return_value='/tmp/cat.jpg'))
        self.stub('send_image_from_gallery',Mock(return_value=False))
        self.run_conversation()
        self.assertTrue(self.q.active('朋友群')); self.assertFalse(self.seen)
        bot.set_pointer.assert_not_called(); bot.send_reply.assert_not_called()

    def test_image_caption_leak_blocks_all_sends(self):
        self.d.update(wants_image=True,image_prompt='小猫')
        bot.leaks_internal.return_value=True
        self.stub('gen_image',Mock())
        self.run_conversation(); bot.gen_image.assert_not_called(); bot.send_reply.assert_not_called()

    def test_full_question_survives_chatter_and_restart(self):
        self.d.update(is_deep_q=True,question_to_answer='新加坡有哪些学校和专业？',question_context='孩子计划留学')
        def failed(d,conv): d['_deep_status']='api_failed'; return '失败'
        self.stub('focused_deep_answer',Mock(side_effect=failed)); self.run_conversation()
        bot.send_reply.assert_not_called(); self.assertTrue(self.q.active('朋友群'))
        with self.q.connect() as db: db.execute('UPDATE jobs SET due=0')
        self.d['last_customer_msg']='今天没喝酒'
        def answer(d,conv):
            self.assertEqual(d['question_to_answer'],'新加坡有哪些学校和专业？')
            self.assertNotIn('喝酒',str(d)); d['_deep_status']='returned'; return '学校答案'
        self.stub('OUTBOX',Outbox(self.q.path)); self.stub('focused_deep_answer',Mock(side_effect=answer))
        bot.send_reply.return_value=True; self.run_conversation()
        self.assertEqual(bot.send_reply.call_args.args[0],'学校答案')

    def test_uncertain_send_only_reconciles(self):
        job=self.q.add('朋友群',self.d); self.q.update(job['id'],'sending')
        self.stub('_reconcile',Mock(return_value=False))
        self.run_conversation(); bot.send_reply.assert_not_called(); bot.set_pointer.assert_not_called()
        self.assertEqual(self.q.get(job['id'])['state'],'sending')

    def test_no_pointer_on_model_failure(self):
        self.d['_skip_seen']=True
        self.run_conversation(); self.assertFalse(self.seen); bot.set_pointer.assert_not_called()

    def test_provider_400_opens_persistent_circuit(self):
        class BadRequest(Exception): status_code=400
        create=Mock(side_effect=BadRequest())
        with self.assertRaises(BadRequest): bot._api_create('claude',create)
        with self.assertRaises(RuntimeError): bot._api_create('claude',create)
        self.assertEqual(create.call_count,1)
        self.assertFalse(Outbox(self.q.path).provider_allowed('claude'))

    def test_total_deadline_interrupts_swallowed_network_loop(self):
        job=self.q.add('朋友群',self.d)
        @bounded(.02,lambda:'pending')
        def slow():
            try: time.sleep(.2)
            except Exception: time.sleep(.2)
        start=time.monotonic(); self.assertEqual(slow(),'pending')
        self.assertLess(time.monotonic()-start,.15)
        self.assertEqual(self.q.get(job['id'])['state'],'pending')

    def test_backoff_is_respected_when_same_message_is_seen_again(self):
        self.run_conversation()
        self.run_conversation()
        self.assertEqual(bot.send_reply.call_count,1)
        bot.set_pointer.assert_not_called()

    def test_multiple_visible_questions_are_persisted(self):
        self.d['pending_questions']=[{'question':'第二个学校问题？','context':'本科','new_direct_mention':False}]
        self.run_conversation()
        with self.q.connect() as db:
            count=db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0]
        self.assertEqual(count,2)

    def test_group_roster_binding_rejects_different_group(self):
        self.assertTrue(self.q.verify_roster('甲、乙…',['甲','乙','本人']))
        self.assertTrue(Outbox(self.q.path).verify_roster('甲、乙…',['本人','乙','甲']))
        self.assertFalse(self.q.verify_roster('甲、乙…',['甲','丙','本人']))

    def test_uncertain_after_crash_never_turns_back_into_ready(self):
        job=self.q.add('朋友群',self.d)
        self.q.update(job['id'],'sending')
        self.stub('_reconcile',Mock(return_value=False))
        bot.OUTBOX=Outbox(self.q.path)
        self.run_conversation()
        self.assertEqual(self.q.get(job['id'])['state'],'sending')
        bot.type_chinese.assert_not_called(); bot.send_reply.assert_not_called()

    def test_strict_boolean_and_stable_named_identity(self):
        with self.assertRaises(ValueError): validate_decision({'need_reply':'false'})
        self.assertTrue(same_conversation('MentionOnly(90)','mentiononly（91）'))
        self.assertFalse(same_conversation('王总','王总朋友群'))


# Load the actual send functions separately so network/UI mocks cannot accidentally hide their behavior.
import ast
source=Path(bot.__file__).read_text()
def raw_function(name):
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name==name)
    ns=dict(bot.__dict__)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<send under test>','exec'),ns)
    return ns[name],ns

class SendSafety(unittest.TestCase):
    setUp = Reliability.setUp
    def test_composer_coordinates_use_actual_pixels(self):
        fn,ns=raw_function('_input_center')
        img=Image.new('RGB',(1080,2400),(237,237,237))
        img.paste('white',(130,2100,830,2200))
        self.assertEqual(fn(img),[445,2150])
        self.assertIsNone(fn(Image.new('RGB',(1080,2400),'black')))

    def test_thumbnail_matches_content_not_first_position(self):
        fn,ns=raw_function('_find_gallery_thumbnail')
        ref=Image.new('RGB',(270,270),'red')
        ref.paste('green',(135,0,270,135));ref.paste('blue',(0,135,135,270))
        gallery=Image.new('RGB',(1080,2400),(35,35,35))
        gallery.paste(ref,(540,500))
        xy=fn(gallery,ref)
        self.assertIsNotNone(xy)
        self.assertLess(abs(xy[0]-761),4);self.assertLess(abs(xy[1]-548),4)
        gallery.paste(ref,(0,232))
        self.assertIsNone(fn(gallery,ref))

    def test_animation_does_not_block_chat_controls(self):
        fn,ns=raw_function('_stable_screen')
        before=Image.new('RGB',(1080,2400),'white'); after=before.copy()
        after.paste('red',(200,700,800,1500))
        self.assertTrue(fn(before,after,chat=True))
        self.assertFalse(fn(before,after))
        after.paste('black',(200,135,800,215))
        self.assertFalse(fn(before,after,chat=True))

    def test_visible_member_prefix_requires_explicit_truncation(self):
        fn,ns=raw_function('_matches_visible_member')
        self.assertTrue(fn('张三丰 PF','张三丰...'))
        self.assertFalse(fn('张三丰 PF','张三'))
        self.assertFalse(fn('张三丰 PF','李三丰...'))

    def test_known_complete_roster_can_resolve_inconsistent_visibility_flag(self):
        fn,ns=raw_function('_verify_identity')
        expected='甲、乙、丙…'
        info=dict(is_group_info=True,all_members_visible=False,member_count=3,members=['甲','乙','本人'])
        ns['_stable_screen']=Mock(return_value=True);ns['_guard_read']=Mock(return_value=info)
        ns['time']=SimpleNamespace(sleep=lambda _:None)
        header=dict(is_chat=True,title='群聊(3)')
        self.assertFalse(fn(self.img,expected,header))  # Unknown groups still require initial full visibility.
        self.q.verify_roster(expected,info['members'])
        self.assertTrue(fn(self.img,expected,header))
        info['members']=['甲','乙','陌生人']
        self.assertFalse(fn(self.img,expected,header))

    def test_truncated_group_requires_enrolled_complete_members(self):
        import json
        fn,ns=raw_function('_verify_identity')
        expected='Alice Adams、Bot Y...'
        info=dict(is_group_info=True,all_members_visible=True,member_count=3,members=['Alice...','Bot...','Charlie...'])
        ns['_stable_screen']=Mock(return_value=True);ns['_guard_read']=Mock(return_value=info)
        ns['time']=SimpleNamespace(sleep=lambda _:None)
        header=dict(is_chat=True,title='群聊(3)')
        self.assertFalse(fn(self.img,expected,header))
        with self.q.connect() as db:
            db.execute('INSERT INTO verified_group_members VALUES(?,?)',('aliceadams、boty...',json.dumps(['Alice Adams','Bot Example','Charlie Brown'])))
        self.assertTrue(fn(self.img,expected,header))
        info['all_members_visible']=False
        self.assertTrue(fn(self.img,expected,header))
        info['members']=['Alice...','Bot...','Unrelated']
        self.assertFalse(fn(self.img,expected,header))

    def test_manual_draft_is_never_typed_or_cleared(self):
        bot._inspect_chat.return_value['draft']='机主写到一半的消息'
        job=self.q.add('朋友群',self.d); send,ns=raw_function('send_reply')
        self.assertFalse(send('你好呀',job))
        bot.type_chinese.assert_not_called(); bot.tap.assert_not_called()

    def test_owned_draft_reused_without_append_and_receipt_required(self):
        self.d['_owned_draft']='你好呀'
        bot._inspect_chat.return_value['draft']='你好呀'
        job=self.q.add('朋友群',self.d); send,ns=raw_function('send_reply')
        ns['_save_before']=Mock(); ns['_stable_screen']=Mock(return_value=True)
        ns['find_send_button']=Mock(return_value=(900,2100)); ns['_receipt']=Mock(return_value=False)
        ns['time']=SimpleNamespace(sleep=lambda _:None)
        self.assertFalse(send('你好呀',job))
        bot.type_chinese.assert_not_called(); self.assertEqual(bot.tap.call_count,1)
        self.assertEqual(self.q.get(job['id'])['state'],'uncertain')

    def test_postscroll_composer_is_rechecked_before_send(self):
        self.d['_owned_draft']='你好呀';job=self.q.add('朋友群',self.d)
        send,ns=raw_function('send_reply')
        original=dict(is_chat=True,title='朋友群',draft='你好呀')
        ns['_inspect_chat']=Mock(side_effect=[original,original,dict(original,draft='本人刚改的内容')])
        ns['_save_before']=Mock();ns['_stable_screen']=Mock(return_value=True)
        ns['time']=SimpleNamespace(sleep=lambda _:None)
        self.assertFalse(send('你好呀',job));bot.tap.assert_not_called()
        self.assertEqual(self.q.get(job['id'])['error'],'final composer changed')

    def test_image_mismatch_never_selects_first_thumbnail(self):
        path=str(Path(self.tmp.name)/'ref.png'); self.img.save(path)
        self.d['_image_path']=path; job=self.q.add('朋友群',self.d)
        send,ns=raw_function('send_image_from_gallery')
        ns['push_to_gallery']=Mock(return_value=True); ns['_stable_screen']=Mock(return_value=True)
        ns['_save_before']=Mock(); ns['time']=SimpleNamespace(sleep=lambda _:None)
        ns['_guard_read']=Mock(return_value={'is_gallery':True,'unique_exact_match':False,'selected_count':0,'checkbox_xy':[200,280]})
        self.assertFalse(send(path,job))
        self.assertEqual(bot.tap.call_count,2)  # Only opens picker, never selects/sends a picture.
        self.assertNotEqual(self.q.get(job['id'])['state'],'sending')

if __name__=='__main__': unittest.main()
