"""Persistent per-conversation circuit breaker; never edits jobs or sends WeChat messages."""
import time
from functools import wraps
from bot_reliability import conversation_key


class ProgressGuard:
    def __init__(self, outbox, clock=time.time):
        self.outbox, self.clock = outbox, clock
        with outbox.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_progress (
                conv TEXT PRIMARY KEY, display TEXT NOT NULL, failures INTEGER NOT NULL,
                until REAL NOT NULL, reason TEXT NOT NULL, updated REAL NOT NULL,
                notice TEXT NOT NULL DEFAULT '')''')

    def status(self, conv):
        with self.outbox.connect() as db:
            row = db.execute('SELECT * FROM conversation_progress WHERE conv=?',
                             (conversation_key(conv),)).fetchone()
        return dict(row) if row else None

    def blocked(self, conv):
        row = self.status(conv)
        return bool(row and row['until'] > self.clock())

    def record(self, conv, action='', error=''):
        if not conv or action.startswith('dry_run_'):
            return
        healthy = action in ('已发送', '已发图', 'skip_无需回复') and not error
        failed = bool(error) or any(word in action for word in (
            '总预算超时', '保留未核实草稿', '会话身份不符', '观察不完整',
            '自动重读', '技术问题已跳过', '失败', '发送未确认', '保留发送核验'))
        # Waiting for debounce, owner input, mention authorization or an existing
        # cooldown is neither progress nor another failure.
        if not healthy and not failed:
            return
        now = self.clock(); key = conversation_key(conv)
        with self.outbox.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM conversation_progress WHERE conv=?', (key,)).fetchone()
            if healthy:
                if old:
                    db.execute("UPDATE conversation_progress SET failures=0,until=0,reason=?,updated=?,notice='' WHERE conv=?",
                               (action, now, key))
                    if old['failures'] >= 3:
                        print('[progress] recovered: %s (%s)' % (conv, action))
                return
            failures = (old['failures'] if old else 0) + 1
            delay = min(1800, 300 * 2 ** min(3, failures - 3)) if failures >= 3 else 0
            notice = old['notice'] if old else ''
            if failures >= 3 and not notice:
                notice = 'pending'
            reason = ('exception:' + error) if error else action
            db.execute('INSERT OR REPLACE INTO conversation_progress VALUES(?,?,?,?,?,?,?)',
                       (key, conv, failures, now + delay if delay else 0, reason, now, notice))
        if delay:
            print('[progress] paused %s for %ss after %s failures: %s' % (conv, delay, failures, reason))

    def notify_one(self, sender):
        # Batch across conversations; a shared navigation failure must not fan
        # out into one notification per group. Persist claim before network I/O.
        now = self.clock()
        with self.outbox.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS progress_notice_window (id INTEGER PRIMARY KEY, attempted REAL NOT NULL)')
            rows = db.execute("SELECT * FROM conversation_progress WHERE notice='pending' ORDER BY updated").fetchall()
            if not rows or now - rows[0]['updated'] < 60:
                return False
            last = db.execute('SELECT attempted FROM progress_notice_window WHERE id=1').fetchone()
            # Include pre-upgrade notices so rollout does not create a new wave.
            legacy = db.execute("SELECT MAX(updated) FROM conversation_progress WHERE notice IN ('sent','attempted','unconfirmed')").fetchone()[0]
            last_at = last[0] if last else (legacy or 0)
            if not last and last_at:
                db.execute('INSERT INTO progress_notice_window VALUES(1,?)', (last_at,))
            if last_at and now-last_at < 1800:
                db.execute("UPDATE conversation_progress SET notice='grouped' WHERE notice='pending'")
                return False
            db.execute('INSERT OR REPLACE INTO progress_notice_window VALUES(1,?)', (now,))
            db.execute("UPDATE conversation_progress SET notice='attempted' WHERE notice='pending'")
        ok = False
        try:
            names = '、'.join(r['display'] for r in rows[:10])
            ok = sender('微信 bot：处理异常汇总', [
                '涉及会话：' + names,
                '连续处理失败，已暂缓相关会话并安排重新检查；其他会话继续。',
                '多个会话一起失败可能是列表定位或读屏的共同问题，不代表每个群都出了问题。',
                '无需逐条确认。30分钟内不重复推送此类提醒；具体原因保留在诊断记录中。']) is True
        except Exception as exc:
            print('[progress] notification failed: ' + type(exc).__name__)
        with self.outbox.connect() as db:
            for row in rows:
                db.execute("UPDATE conversation_progress SET notice=? WHERE conv=? AND notice='attempted' AND updated=?",
                           ('sent' if ok else 'unconfirmed', row['conv'], row['updated']))
        return True


def guarded(outbox):
    """Wrap OUTSIDE the total deadline so timeout results are counted too."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            conv = kwargs.get('conv_hint') or (args[1] if len(args) > 1 else '')
            guard = ProgressGuard(outbox())
            if guard.blocked(conv):
                return {'_action': '待答_持续故障退避'}
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                guard.record(conv, error=type(exc).__name__)
                raise
            guard.record(conv, action=(result or {}).get('_action', ''))
            return result
        return run
    return decorate


def notification_worker(outbox, sender):
    while True:
        try:
            ProgressGuard(outbox()).notify_one(sender)
        except Exception as exc:
            print('[progress] notification worker: ' + type(exc).__name__)
        time.sleep(15)
