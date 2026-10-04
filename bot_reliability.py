"""Durable reply transactions. No device/network actions in this module."""
import functools
import hashlib
import json
import os
import re
import signal
import sqlite3
import time
from contextlib import contextmanager


def conversation_key(name):
    # Only normalize typography and the trailing group member count. No fuzzy aliases.
    return re.sub(r'[（(]\d+[）)]$', '', re.sub(r'\s+', '', (name or '').replace('…', '...'))).casefold()


def same_conversation(a, b):
    return bool(conversation_key(a)) and conversation_key(a) == conversation_key(b)


def generic_title(name):
    return bool(re.fullmatch(r'(群聊|群|微信对话|未知对话|聊天)(?:[（(]\d+[）)])?', name or ''))


class Outbox:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, conv TEXT NOT NULL, display TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                due REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, error TEXT DEFAULT '');
              CREATE INDEX IF NOT EXISTS job_due ON jobs(conv,state,due);
              CREATE TABLE IF NOT EXISTS observations (conv TEXT PRIMARY KEY, display TEXT, due REAL);
              CREATE TABLE IF NOT EXISTS mentions (conv TEXT PRIMARY KEY, display TEXT, created REAL);
              CREATE TABLE IF NOT EXISTS providers (name TEXT PRIMARY KEY, until REAL, error TEXT);
              CREATE TABLE IF NOT EXISTS identities (conv TEXT PRIMARY KEY, roster TEXT NOT NULL);
            ''')
            import reply_metrics
            reply_metrics.init(db)
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def note_observation(self, conv):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)', (conversation_key(conv), conv, time.time()))

    def defer_observation(self, conv, delay=90):
        with self.connect() as db:
            db.execute('UPDATE observations SET due=? WHERE conv=?', (time.time()+delay, conversation_key(conv)))

    def resolve_observation(self, conv):
        with self.connect() as db:
            db.execute('DELETE FROM observations WHERE conv=?', (conversation_key(conv),))

    def note_mention(self, conv):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO mentions VALUES (?,?,?)',
                       (conversation_key(conv), conv, time.time()))

    def has_mention(self, conv):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM mentions WHERE conv=?', (conversation_key(conv),)).fetchone() is not None

    def clear_mention(self, conv):
        with self.connect() as db:
            db.execute('DELETE FROM mentions WHERE conv=?', (conversation_key(conv),))

    def add(self, conv, payload):
        now = time.time()
        # Persist one event while pending, even when new chatter changes the last visible bubble.
        question = payload.get('question_to_answer') or payload.get('last_customer_msg') or ''
        event = payload.get('message_event') or payload.get('_fingerprint') or question
        if payload.get('is_deep_q') is True and question and not payload.get('_request_id'):
            with self.connect() as db:
                active = db.execute("SELECT id,payload FROM jobs WHERE conv=? AND state NOT IN ('sent','ignored','human')", (conversation_key(conv),)).fetchall()
            for row in active:
                if json.loads(row['payload']).get('question_to_answer') == question:
                    return self.get(row['id'])
            event = payload.get('message_event') or question
        jid = hashlib.sha256((conversation_key(conv) + '\0' + event).encode()).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO jobs(id,conv,display,payload,state,due,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (jid, conversation_key(conv), conv, json.dumps(payload, ensure_ascii=False), 'pending', now, now, now))
        return self.get(jid)

    def get(self, jid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            return None
        row = dict(row); row['payload'] = json.loads(row['payload'])
        return row

    def _open_turns(self, db, conv=None):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_requests'").fetchone():
            return []
        return db.execute("SELECT display FROM turn_requests WHERE (state='open' OR (state='review' AND reason='observation gap'))" + (' AND conv=?' if conv else ''),
                          (conversation_key(conv),) if conv else ()).fetchall()

    def next(self, conv):
        with self.connect() as db:
            # Any unresolved send blocks later delivery, even during reconciliation backoff.
            uncertain = db.execute("SELECT id,due FROM jobs WHERE conv=? AND state IN ('sending','uncertain') ORDER BY created LIMIT 1", (conversation_key(conv),)).fetchone()
            if uncertain:
                return self.get(uncertain['id']) if uncertain['due'] <= time.time() else None
            rows = db.execute("SELECT id,payload,created FROM jobs WHERE conv=? AND state IN ('pending','ready','prepared') AND due<=?", (conversation_key(conv), time.time())).fetchall()
        rows = sorted(rows, key=lambda r: (json.loads(r['payload']).get('_priority', 1), r['created']))
        return self.get(rows[0]['id']) if rows else None

    def active(self, conv):
        with self.connect() as db:
            return bool(self._open_turns(db, conv)) or db.execute("SELECT 1 FROM jobs WHERE conv=? AND state NOT IN ('sent','ignored','human') LIMIT 1", (conversation_key(conv),)).fetchone() is not None

    def targets(self):
        with self.connect() as db:
            rows = db.execute("SELECT display FROM jobs WHERE state NOT IN ('sent','ignored','human') AND due<=? UNION SELECT display FROM mentions UNION SELECT display FROM observations WHERE due<=?", (time.time(), time.time())).fetchall()
            rows += self._open_turns(db)
        return list(dict.fromkeys(r['display'] for r in rows))

    def update(self, jid, state, payload=None, error='', delay=0):
        now = time.time()
        with self.connect() as db:
            if payload is not None:
                db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), jid))
            db.execute('UPDATE jobs SET state=?,due=?,updated=?,error=?,attempts=attempts+? WHERE id=?',
                       (state, now+delay, now, error[:300], int(bool(error)), jid))
            if state == 'sent':
                import reply_metrics
                try:
                    reply_metrics.confirmed(db, jid, now)
                except Exception as exc:
                    print('[metrics] receipt write failed '+type(exc).__name__)
            if state in ('sent', 'ignored'):
                # Only this confirmed mention event is consumed; plain jobs never grant mention permission.
                row = db.execute('SELECT conv,payload FROM jobs WHERE id=?', (jid,)).fetchone()
                if row and json.loads(row['payload']).get('_mention_authorized') is True and not json.loads(row['payload']).get('_request_id'):
                    db.execute('DELETE FROM mentions WHERE conv=?', (row['conv'],))

    def verify_roster(self, conv, members):
        roster = json.dumps(sorted(conversation_key(m) for m in members), ensure_ascii=False)
        with self.connect() as db:
            row = db.execute('SELECT roster FROM identities WHERE conv=?', (conversation_key(conv),)).fetchone()
            if row:
                return row['roster'] == roster
            db.execute('INSERT INTO identities VALUES(?,?)', (conversation_key(conv), roster))
            return True

    def provider_allowed(self, name):
        with self.connect() as db:
            row = db.execute('SELECT until FROM providers WHERE name=?', (name,)).fetchone()
        return not row or row['until'] <= time.time()

    def provider_succeeded(self, name):
        with self.connect() as db:
            db.execute('DELETE FROM providers WHERE name=?', (name,))

    def provider_failed(self, name, exc):
        status = getattr(exc, 'status_code', None)
        delay = 3600 if status in (400, 401, 403, 404) else (300 if status == 429 else 180)
        # Record error class/status, never credentials or response bodies.
        error = '%s HTTP %s' % (type(exc).__name__, status)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO providers VALUES(?,?,?)', (name, time.time()+delay, error))


class DeadlineExceeded(BaseException):
    """Bypass broad SDK fallback catches; handled at the outer work-unit boundary."""


@contextmanager
def deadline(seconds):
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(*_):
        raise DeadlineExceeded('work unit exceeded %.0fs' % seconds)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, old_timer[0] - (time.monotonic()-started)), old_timer[1])


def bounded(seconds, fallback):
    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            limit = min(seconds, max(.001, kwargs.pop('_budget_seconds', seconds)))
            try:
                with deadline(limit):
                    return fn(*args, **kwargs)
            except DeadlineExceeded:
                print('[deadline] %s exceeded %ss; pending work retained' % (fn.__name__, limit))
                return fallback()  # Never marks a pending event complete.
        return run
    return decorate


def validate_decision(value):
    if not isinstance(value, dict):
        raise ValueError('decision must be an object')
    for key in ('need_reply', 'last_from_customer', 'is_deep_q', 'new_direct_mention',
                'is_official', 'needs_human', 'wants_image', 'offer_image', 'needs_image'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('invalid boolean: ' + key)
    for key in ('conversation', 'last_customer_msg', 'reply', 'question_to_answer',
                'question_context', 'image_prompt', 'message_event', 'owner_question', 'reason'):
        if key in value and not isinstance(value[key], str):
            raise ValueError('invalid text: ' + key)
    return value


class WebAnswer(str):
    def __new__(cls, text, sources, provider):
        verified_at = time.time()
        suffix = "\n来源（核查于" + time.strftime('%Y-%m-%d') + "）：\n" + "\n".join(sources[:5])
        obj = str.__new__(cls, text + suffix)
        obj.sources = list(sources)
        obj.provider = provider
        obj.verified_at = verified_at
        return obj
