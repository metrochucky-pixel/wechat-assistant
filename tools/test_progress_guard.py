"""Failure injection only; no device, model API or real notifications."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_reliability import Outbox, bounded
from bot_progress import ProgressGuard, guarded


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.out = Outbox(self.tmp.name + '/db')
        self.now = 1000
        self.g = ProgressGuard(self.out, clock=lambda: self.now)

    def notify(self, sender):
        self.now += 61
        return self.g.notify_one(sender)

    def fail(self, count=3, action='待答_保留未核实草稿'):
        for _ in range(count): self.g.record('朋友群', action)

    def test_three_failures_pause_only_one_group(self):
        self.fail(2); self.assertFalse(self.g.blocked('朋友群'))
        self.fail(1); self.assertTrue(self.g.blocked('朋友群'))
        self.assertFalse(self.g.blocked('其他群'))
        self.assertEqual(self.g.status('朋友群')['until'], 1300)

    def test_restart_keeps_cooldown_and_notification_claim(self):
        self.fail(); sender = Mock(return_value=True); self.notify(sender)
        restarted = ProgressGuard(Outbox(self.out.path), clock=lambda: self.now)
        self.assertTrue(restarted.blocked('朋友群'))
        self.assertFalse(restarted.notify_one(sender)); sender.assert_called_once()

    def test_probe_failure_extends_backoff_with_cap(self):
        self.fail(); self.now = 1300; self.assertFalse(self.g.blocked('朋友群'))
        self.fail(1); self.assertEqual(self.g.status('朋友群')['until'], 1900)
        self.fail(20); self.assertEqual(self.g.status('朋友群')['until'], self.now + 1800)

    def test_intentional_silence_is_not_failure(self):
        for action in ('skip_仅被@回复', '待确认_已排队通知机主', '待答_合并补充或退避', 'dry_run_发送失败'):
            self.fail(4, action)
        self.assertIsNone(self.g.status('朋友群'))

    def test_wait_does_not_reset_existing_incident(self):
        self.fail(); self.g.record('朋友群', '待答_合并补充或退避')
        self.assertEqual(self.g.status('朋友群')['failures'], 3)

    def test_healthy_observation_closes_without_claiming_a_send(self):
        self.fail(); self.g.record('朋友群', 'skip_无需回复')
        status = self.g.status('朋友群')
        self.assertEqual(status['failures'], 0); self.assertEqual(status['reason'], 'skip_无需回复')
        self.assertFalse(self.notify(Mock()))

    def test_one_notice_per_incident_and_new_notice_after_recovery(self):
        sender = Mock(return_value=True)
        self.fail(); self.notify(sender); self.fail(3); self.notify(sender)
        sender.assert_called_once()
        self.g.record('朋友群', '已发送'); self.now += 1801; self.fail(); self.notify(sender)
        self.assertEqual(sender.call_count, 2)

    def test_uncertain_notification_is_not_automatically_resent(self):
        sender = Mock(side_effect=TimeoutError)
        self.fail(); self.notify(sender); self.notify(sender)
        self.assertEqual(self.g.status('朋友群')['notice'], 'unconfirmed')
        sender.assert_called_once()

    def test_failed_sender_is_not_claimed_successful(self):
        self.fail(); self.notify(Mock(return_value=False))
        self.assertEqual(self.g.status('朋友群')['notice'], 'unconfirmed')

    def test_many_groups_generate_one_batch_after_collection_window(self):
        self.fail()
        for _ in range(3):self.g.record('另一个群','skip_会话身份不符或未确认')
        sender=Mock(return_value=True)
        self.assertFalse(self.g.notify_one(sender))
        self.notify(sender);sender.assert_called_once()
        self.assertIn('另一个群',sender.call_args.args[1][0])
        self.assertEqual(self.g.status('另一个群')['notice'],'sent')

    def test_new_group_does_not_start_second_wave_inside_window(self):
        sender=Mock(return_value=True);self.fail();self.notify(sender)
        for _ in range(3):self.g.record('另一个群','skip_会话身份不符或未确认')
        self.notify(sender);sender.assert_called_once()
        self.assertEqual(self.g.status('另一个群')['notice'],'grouped')

    def test_no_mutation_of_uncertain_send_or_old_draft(self):
        self.out.add('朋友群', {'reply':'原草稿', '_owned_draft':'原草稿', '_cleanup_draft':True})
        job = self.out.next('朋友群'); self.out.update(job['id'], 'uncertain')
        before = self.out.get(job['id'])
        self.fail(action='发送未确认'); self.now += 300
        self.assertFalse(self.g.blocked('朋友群'))
        self.assertEqual(self.out.get(job['id']), before)

    def test_cooldown_wrapper_never_calls_phone_work(self):
        live = ProgressGuard(self.out)
        for _ in range(3): live.record('朋友群', '待答_观察不完整')
        phone_work = Mock(return_value={'_action':'已发送'})
        wrapped = guarded(lambda:self.out)(phone_work)
        self.assertEqual(wrapped(set(), conv_hint='朋友群')['_action'], '待答_持续故障退避')
        phone_work.assert_not_called()
        wrapped(set(), conv_hint='其他群'); phone_work.assert_called_once()

    def test_total_deadline_result_counted_outside_budget_wrapper(self):
        import time
        work = bounded(.005, lambda:{'_action':'待答_总预算超时'})(lambda *a, **k:time.sleep(.1))
        wrapped = guarded(lambda:self.out)(work)
        for _ in range(3): wrapped(set(), conv_hint='朋友群')
        self.assertTrue(ProgressGuard(self.out).blocked('朋友群'))

    def test_exceptions_counted_but_interruption_does_not(self):
        work = guarded(lambda:self.out)(Mock(side_effect=RuntimeError))
        with self.assertRaises(RuntimeError): work(set(), '朋友群')
        self.assertEqual(self.g.status('朋友群')['failures'], 1)
        stop = guarded(lambda:self.out)(Mock(side_effect=KeyboardInterrupt))
        with self.assertRaises(KeyboardInterrupt): stop(set(), '朋友群')
        self.assertEqual(self.g.status('朋友群')['failures'], 1)


if __name__ == '__main__': unittest.main()
