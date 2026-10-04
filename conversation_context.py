"""Bounded, extractive context. Exact conversation isolation; never grants authority."""
import json
import re
import time


def terms(text):
    parts = re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]+', str(text).lower())
    return {token for p in parts for token in ([p] if p.isascii() else [p[i:i+2] for i in range(len(p)-1)])}


def related(question, other):
    a, b = terms(question), terms(other)
    return len(a & b) >= 2 and len(a & b) / max(1, min(len(a), len(b))) >= .25


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS context_turns(
        job_id TEXT PRIMARY KEY, conv TEXT, sender TEXT, question TEXT, reply TEXT, confirmed REAL)''')
    db.execute('CREATE INDEX IF NOT EXISTS context_conv ON context_turns(conv,confirmed)')


def remember(outbox, job):
    d = job['payload']
    with outbox.connect() as db:
        init(db)
        # Only verified, durably recorded sends may become conversational history.
        row = db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if not row or row['state'] != 'sent':
            return
        db.execute('INSERT OR IGNORE INTO context_turns VALUES(?,?,?,?,?,?)',
            (job['id'], job['display'], d.get('_request_sender', ''),
             d.get('question_to_answer') or d.get('last_customer_msg', ''), d.get('reply') or '(已发送图片)', time.time()))
        db.execute('DELETE FROM context_turns WHERE confirmed<?', (time.time()-30*86400,))
        db.execute('DELETE FROM context_turns WHERE job_id IN (SELECT job_id FROM context_turns ORDER BY confirmed DESC LIMIT -1 OFFSET 1000)')


def build(outbox, conv, payload, messages=(), legacy=()):
    """Keep the active question/constraints verbatim; whole historical items fit a budget."""
    question = payload.get('question_to_answer') or payload.get('last_customer_msg', '')
    sender = payload.get('_request_sender', '')
    with outbox.connect() as db:
        init(db)
        rows = [dict(r) for r in db.execute('SELECT * FROM context_turns WHERE conv=? AND confirmed>? ORDER BY confirmed DESC LIMIT 40', (conv, time.time()-30*86400))]
    recent_rows = list(rows)
    rows.sort(key=lambda row: bool(sender and sender == row["sender"]), reverse=True)
    history, used = [], 0
    for row in rows:
        if not related(question, row['question']):
            continue
        item = {'sender': row['sender'], 'question': row['question'], 'reply': row['reply'],
                'same_requester': bool(sender and sender == row['sender'])}
        size = len(json.dumps(item, ensure_ascii=False))
        if used+size <= 1800:
            history.append(item); used += size
        if len(history) == 4:
            break
    # Legacy history lacks reliable requester identity: use only exact-group, topical lines.
    for line in list(legacy)[-8:][::-1]:
        if not rows and related(question, line) and used+len(line)+40 <= 1800:
            history.append({'legacy_requester_unknown': line}); used += len(line)+40
    selected, size = [], 0
    for message in reversed(list(messages)):
        if not isinstance(message, dict):
            continue
        if message.get('sender') != sender and not related(question, message.get('text', '')+' '+message.get('quote', '')):
            continue
        n = len(json.dumps(message, ensure_ascii=False))
        if size+n <= 2200:
            selected.append(message); size += n
    # This is an anti-repetition hint, not a source of identity or personal facts.
    recent = []
    for row in recent_rows[:3]:
        if len(row['reply']) <= 250:
            recent.append(row['reply'])
    return {'question': question, 'context': payload.get('question_context', ''), 'requester': sender,
            'messages': list(reversed(selected)), 'related_history': history,
            'recent_replies_do_not_repeat': recent, 'omitted_messages': len(messages)-len(selected),
            'scope': '仅当前会话；历史仅作背景，不是当前请求或授权。提问者不同不可合并其经历或要求。'}
