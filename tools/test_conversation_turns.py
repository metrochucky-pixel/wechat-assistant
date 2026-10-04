"""Offline multi-speaker replay; no phone, API, or production database access."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox, conversation_key
from conversation_planner import TurnPlanner


def msg(sender, text, mention=False, self_=False):
    return dict(sender=sender, text=text, quote='', self=self_, direct_mention=mention, certain=True)


def action(kind, events, sender='甲', question='新加坡本科', rid='', qkind='answer'):
    return dict(action=kind, event_ids=[e['id'] for e in events], sender=sender, question=question,
        request_id=rid, context='', kind=qkind, image_prompt='', addressed_to_bot=True, reason='explicit requester evidence')


class Turns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Outbox(self.tmp.name+'/out.sqlite')
        self.now = 1000.
        self.p = TurnPlanner(self.out, clock=lambda: self.now)
        self.conv = 'MentionOnly'
        self.base = [msg('自己', '已经答完了', self_=True)]
        self.p.ingest(self.conv, self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def new(self, text='@Bot Example 新加坡本科', sender='甲', strict=True):
        self.base.append(msg(sender, text, mention=text.startswith('@Bot Example')))
        ob = self.p.ingest(self.conv, self.base)
        self.p.apply(self.conv, ob, [action('new', ob['events'], sender, text)], strict)
        return self.p.requests(self.conv)

    def enqueue(self):
        self.now += 7
        self.p.enqueue(self.conv, True)
        return self.out.next(self.conv)

    def test_first_screen_is_baseline_not_historical_replay(self):
        p = TurnPlanner(self.out)
        ob = p.ingest('other', [msg('甲', '@Bot Example 旧问题', True)])
        self.assertFalse(ob['continuous']); self.assertEqual(ob['events'], [])

    def test_same_screen_and_restart_do_not_duplicate(self):
        self.new(); self.enqueue()
        p = TurnPlanner(self.out, clock=lambda: self.now)
        ob = p.ingest(self.conv, self.base)
        self.assertEqual(ob['events'], [])
        p.enqueue(self.conv, True)
        with self.out.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_interleaved_supplements_merge_without_chatter(self):
        r = self.new()[0]; self.now += 2
        self.base += [msg('乙', '吃饭了吗'), msg('甲', '计算机，预算有限')]
        ob = self.p.ingest(self.conv, self.base)
        self.p.apply(self.conv, ob, [action('ignore', ob['events'][:1], '乙'),
            action('update', ob['events'][1:], question='新加坡本科计算机，预算有限', rid=r['id'])], True)
        rows = self.p.requests(self.conv)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['revision'], 2)
        job = self.enqueue(); self.assertNotIn('吃饭', job['payload']['question_to_answer'])

    def test_a_mention_never_authorizes_b(self):
        self.new(); rows = self.new('另一个问题', '乙')
        self.assertEqual([r['sender'] for r in rows], ['甲'])

    def test_two_same_questions_keep_two_requesters_and_auth(self):
        self.new(); self.new(sender='乙'); job = self.enqueue()
        with self.out.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.out.update(job['id'], 'sent', job['payload']); self.p.complete(job)
        self.assertEqual(len(self.p.requests(self.conv)), 1)
        self.assertTrue(self.p.valid(self.out.next(self.conv), True))

    def test_correction_invalidates_generated_answer(self):
        r = self.new()[0]; job = self.enqueue()
        job['payload']['reply'] = '本科旧答案'; self.out.update(job['id'], 'ready', job['payload'])
        self.base.append(msg('甲', '改成硕士'))
        ob = self.p.ingest(self.conv, self.base)
        self.p.apply(self.conv, ob, [action('update', ob['events'], question='新加坡硕士', rid=r['id'])], True)
        self.assertFalse(self.p.valid(job, True, commit=True))
        self.assertEqual(self.out.get(job['id'])['state'], 'ignored')
        self.now += 7; self.p.enqueue(self.conv, True)
        self.assertEqual(self.out.next(self.conv)['payload']['question_to_answer'], '新加坡硕士')

    def test_cooldown_does_not_stop_new_event_ingestion(self):
        self.new(); job = self.enqueue(); self.out.update(job['id'], 'pending', delay=180)
        self.new('@Bot Example 第二个问题', '乙')
        self.assertEqual(len(self.p.requests(self.conv)), 2)

    def test_uncertain_send_blocks_next_even_during_backoff(self):
        self.new(); first = self.enqueue()
        self.out.update(first['id'], 'uncertain', delay=300)
        self.new('@Bot Example 第二个问题', '乙'); self.enqueue()
        self.assertIsNone(self.out.next(self.conv))

    def test_no_mention_no_jobs(self):
        self.new('问个问题'); self.enqueue()
        self.assertFalse(self.out.active(self.conv)); self.assertIsNone(self.out.next(self.conv))

    def test_other_person_cannot_cancel_or_supply_constraints(self):
        r = self.new()[0]
        self.base.append(msg('乙', '算了改硕士'))
        ob = self.p.ingest(self.conv, self.base)
        for kind in ('cancel', 'update', 'resolved'):
            with self.assertRaises(ValueError):
                self.p.apply(self.conv, ob, [action(kind, ob['events'], rid=r['id'])], True)
        self.assertEqual(self.p.requests(self.conv)[0]['revision'], 1)

    def test_acknowledged_answer_or_cancel_withdraws(self):
        for kind in ('resolved', 'cancel'):
            with self.subTest(kind=kind):
                r = self.new('@Bot Example 问题'+kind)[-1]; job = self.enqueue()
                self.base.append(msg('甲', '明白了，谢谢' if kind=='resolved' else '不用回答了'))
                ob = self.p.ingest(self.conv, self.base)
                self.p.apply(self.conv, ob, [action(kind, ob['events'], rid=r['id'])], True)
                self.assertFalse(self.p.valid(job, True))

    def test_unknown_sender_fails_closed(self):
        self.base.append(msg('', '@Bot Example 问题', True)); ob = self.p.ingest(self.conv, self.base)
        with self.assertRaises(ValueError):
            self.p.apply(self.conv, ob, [action('new', ob['events'], '')], True)

    def test_fabricated_event_rolls_back_entire_plan(self):
        self.base.append(msg('甲', '@Bot Example 问题', True)); ob = self.p.ingest(self.conv, self.base)
        bad = action('new', [{'id':'fabricated'}])
        with self.assertRaises(ValueError):
            self.p.apply(self.conv, ob, [action('new', ob['events']), bad], True)
        self.assertEqual(self.p.requests(self.conv), [])

    def test_gapped_screen_does_not_replay(self):
        ob = self.p.ingest(self.conv, [msg('丙', '@Bot Example 另一屏', True)])
        self.assertFalse(ob['continuous']); self.assertEqual(ob['events'], [])

    def test_owned_draft_correction_requires_review(self):
        r = self.new()[0]; job = self.enqueue()
        job['payload']['_owned_draft'] = '旧答案'; self.out.update(job['id'], 'prepared', job['payload'])
        self.base.append(msg('甲', '改硕士')); ob = self.p.ingest(self.conv, self.base)
        self.p.apply(self.conv, ob, [action('update', ob['events'], rid=r['id'])], True)
        self.assertEqual(self.out.get(job['id'])['state'], 'human')
        self.assertFalse(self.p.valid(job, True))

    def test_reply_requires_new_mention_after_completion(self):
        self.new(); job = self.enqueue(); self.out.update(job['id'], 'sent'); self.p.complete(job)
        self.new('还有学费呢')
        self.assertEqual(self.p.requests(self.conv), [])

    def test_repeated_identical_new_text_not_permanent_hash_dedup(self):
        self.new(); job = self.enqueue(); self.out.update(job['id'], 'sent'); self.p.complete(job)
        self.base += [msg('自己', '答完', self_=True)]
        ob = self.p.ingest(self.conv, self.base)
        self.p.apply(self.conv, ob, [action('ignore', ob['events'])], True)
        self.new()
        self.assertEqual(len(self.p.requests(self.conv)), 1)

    def test_priority_mentions_before_ordinary_questions(self):
        self.new('一般问题', strict=False)
        self.new('@Bot Example 急问', '乙')
        self.now += 7; self.p.enqueue(self.conv, False)
        self.assertEqual(self.out.next(self.conv)['payload']['question_to_answer'], '@Bot Example 急问')

    def test_commit_checks_policy_and_request_version(self):
        self.new(); job=self.enqueue(); job['payload']['_turn_policy']='old'
        self.assertFalse(self.p.valid(job, True, commit=True))
        self.assertNotEqual(self.out.get(job['id'])['state'], 'sending')

    def test_verbatim_anchor_survives_wrong_self_classification(self):
        conv='anchor'
        old=msg('张三丰','我爱我的祖国')
        initial=self.p.ingest(conv,[old])
        changed=msg('','我爱我的祖国',self_=True)
        ob=self.p.ingest(conv,[changed,msg('示例群友','关键我在新加坡，可以直接看')])
        self.assertTrue(ob['continuous'])
        self.assertEqual(len(ob['events']),1)
        self.assertEqual(ob['visible'][0]['message']['sender'],'张三丰')
        self.assertFalse(ob['visible'][0]['message']['self'])

    def test_short_or_media_only_anchors_do_not_bridge_identity_drift(self):
        for text in ('好','[图片]'):
            conv='weak'+text
            self.p.ingest(conv,[msg('甲',text)])
            ob=self.p.ingest(conv,[msg('',text,self_=True),msg('乙','另一个问题')])
            self.assertFalse(ob['continuous']);self.assertEqual(ob['events'],[])

    def test_ordered_sequence_bridges_cropped_sender_without_replaying(self):
        conv='sequence'
        a=msg('','顶部裁剪了昵称');a['certain']=False
        old=[a,msg('甲','[图片]'),msg('甲','汤唯'),msg('甲','[图片]'),msg('乙','哈哈哈哈')]
        self.p.ingest(conv,old)
        new=[dict(a,sender='甲',certain=True)]+old[1:]
        ob=self.p.ingest(conv,new)
        self.assertTrue(ob['continuous']);self.assertEqual(ob['events'],[])

    def test_expiry_does_not_abandon_an_owned_draft(self):
        ob=self.p.ingest(self.conv,self.base+[msg('甲','@Bot Example 哈哈哈哈',True)])
        a=action('new',ob['events'],qkind='chat')
        self.p.apply(self.conv,ob,[a],True);j=self.enqueue()
        j['payload']['_owned_draft']='已经输入的回复'
        self.out.update(j['id'],'prepared',j['payload'])
        self.now+=400;self.p.enqueue(self.conv,True)
        self.assertTrue(self.p.valid(self.out.get(j['id']),True))

    def test_social_reaction_is_a_turn_even_without_a_question(self):
        ob=self.p.ingest(self.conv,self.base+[msg('甲','哈哈哈哈')])
        a=action('new',ob['events'],qkind='chat',question='')
        a.update(join_chat=True,addressed_to_bot=False,context='大家在笑图片中的玩偶抢镜')
        self.p.apply(self.conv,ob,[a],False,allow_smalltalk=True)
        r=self.p.requests(self.conv)[0]
        self.assertIn('哈哈哈哈',r['question']);self.assertIn('玩偶',r['context'])

    def test_social_join_only_for_allowed_groups_and_chat_kind(self):
        for allow, strict, kind, expected in [(True,False,'chat',1),(False,False,'chat',0),(True,True,'chat',0),(True,False,'answer',0)]:
            with self.subTest(allow=allow,strict=strict,kind=kind):
                conv='social-'+str((allow,strict,kind))
                self.p.ingest(conv,self.base)
                ob=self.p.ingest(conv,self.base+[msg('甲','明天放假了')])
                a=action('new',ob['events'],qkind=kind);a.update(addressed_to_bot=False,join_chat=True)
                self.p.apply(conv,ob,[a],strict,allow_smalltalk=allow)
                self.assertEqual(len(self.p.requests(conv)),expected)

    def test_migration_preserves_uncertainty_not_legacy_authorization(self):
        j=self.out.add(self.conv, {'message_event':'legacy', '_mention_authorized':True})
        self.p.quarantine_legacy(self.conv)
        self.assertEqual(self.out.get(j['id'])['state'],'human')
        j=self.out.add(self.conv, {'message_event':'uncertain'}); self.out.update(j['id'],'uncertain')
        self.p.quarantine_legacy(self.conv)
        self.assertEqual(self.out.get(j['id'])['state'],'uncertain')

class CroppedContext(unittest.TestCase):
    def test_changed_cropped_prefix_keeps_pending_tail_and_only_new_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            p=TurnPlanner(Outbox(d+'/db'))
            tail=msg('甲','这个我还第一次见 准备买一瓶试试')
            p.ingest('群',[msg('','old cropped article'),tail])
            same=p.ingest('群',[msg('','different OCR on cropped article'),tail])
            self.assertTrue(same['continuous']);self.assertEqual(same['events'],[])
            new=p.ingest('群',[msg('','another crop'),tail,msg('乙','我也没听说过')])
            self.assertTrue(new['continuous']);self.assertEqual([e['text'] for e in new['events']],['我也没听说过'])
    def test_fullwidth_tilde_and_spaces_do_not_erase_current_turn(self):
        with tempfile.TemporaryDirectory() as d:
            p=TurnPlanner(Outbox(d+'/db'))
            p.ingest('群',[msg('','截断文章甲'),msg('平Pingan','我也没听说过～～')])
            ob=p.ingest('群',[msg('','截断文章乙'),msg('平Pingan','我也没听说过~ ~')])
            self.assertTrue(ob['continuous']);self.assertEqual(ob['events'],[])

    def test_duplicate_tail_anchor_stays_ambiguous(self):
        with tempfile.TemporaryDirectory() as d:
            p=TurnPlanner(Outbox(d+'/db'));tail=msg('甲','这是重复的完整长句子')
            p.ingest('群',[msg('','old'),tail])
            ob=p.ingest('群',[msg('','new'),tail,tail]);self.assertFalse(ob['continuous'])

if __name__ == '__main__': unittest.main()
