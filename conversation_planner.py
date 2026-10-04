"""Evidence-bound conversation turns. No network or phone operations."""
import hashlib
import json
import time
import uuid
import unicodedata
import runtime_settings
from bot_reliability import conversation_key

POLICY = 'turns-v1'
TERMINAL = ('sent', 'ignored', 'human')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean_messages(value):
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError('invalid message list')
    result = []
    for m in value:
        if not isinstance(m, dict):
            raise ValueError('invalid message')
        if any(not isinstance(m.get(k), str) for k in ('sender', 'text', 'quote')):
            raise ValueError('missing verbatim message fields')
        if any(type(m.get(k)) is not bool for k in ('self', 'direct_mention', 'certain')):
            raise ValueError('invalid message flags')
        if not m['text'].strip() or len(m['text']) > 8000:
            raise ValueError('invalid message text')
        nickname=''.join(runtime_settings.self_name().casefold().split())
        if m['direct_mention'] and (not nickname or '@'+nickname not in ''.join(m['text'].casefold().split()) or m['self']):
            raise ValueError('mention without visible evidence')
        result.append({k: m[k] for k in ('sender', 'text', 'quote', 'self', 'direct_mention', 'certain')})
    return result


class TurnPlanner:
    def __init__(self, outbox, clock=time.time):
        self.outbox, self.clock = outbox, clock
        with outbox.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS turn_screen_cache(conv TEXT PRIMARY KEY, digest TEXT, payload TEXT);
              CREATE TABLE IF NOT EXISTS turn_screens(conv TEXT PRIMARY KEY, messages TEXT, updated REAL);
              CREATE TABLE IF NOT EXISTS turn_events(id TEXT PRIMARY KEY, conv TEXT, payload TEXT, observed REAL);
              CREATE INDEX IF NOT EXISTS turn_event_conv ON turn_events(conv,observed);
              CREATE TABLE IF NOT EXISTS turn_requests(
                id TEXT PRIMARY KEY, conv TEXT, display TEXT, sender TEXT, question TEXT, context TEXT,
                kind TEXT, image_prompt TEXT, revision INTEGER, state TEXT, priority INTEGER,
                sources TEXT, authorization TEXT, created REAL, relevant REAL, eligible REAL, reason TEXT);
              CREATE INDEX IF NOT EXISTS turn_request_conv ON turn_requests(conv,state,eligible);
              CREATE TABLE IF NOT EXISTS turn_audit(id INTEGER PRIMARY KEY, conv TEXT, observed REAL, detail TEXT);
              CREATE TABLE IF NOT EXISTS turn_recoveries(conv TEXT, stamp TEXT, event_id TEXT, planned INTEGER DEFAULT 0, PRIMARY KEY(conv,stamp));
              CREATE TABLE IF NOT EXISTS turn_reads(conv TEXT PRIMARY KEY, event_ids TEXT, planned INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS turn_recovery_events(conv TEXT,event_id TEXT,attempts INTEGER,done INTEGER,PRIMARY KEY(conv,event_id));
              CREATE INDEX IF NOT EXISTS recovery_by_event ON turn_recoveries(conv,event_id,planned);
              CREATE TABLE IF NOT EXISTS gap_reviews(request_id TEXT PRIMARY KEY,attempts INTEGER,checked REAL);
            ''')

    def maintain_gaps(self, conv=None, observed=False):
        """Bound technical quarantine. Never revive an answer across missing messages."""
        now = self.clock()
        with self.outbox.connect() as db:
            rows=db.execute("SELECT * FROM turn_requests WHERE state='review' AND reason='observation gap'" +
                (' AND conv=?' if conv else ''), (conversation_key(conv),) if conv else ()).fetchall()
            for r in rows:
                jobs=db.execute('SELECT state,payload FROM jobs WHERE conv=?',(r['conv'],)).fetchall()
                related=[j for j in jobs if json.loads(j['payload']).get('_request_id')==r['id']]
                if any(j['state']=='sent' for j in related):
                    db.execute("UPDATE turn_requests SET state='sent',reason='confirmed receipt reconciled' WHERE id=?",(r['id'],))
                    continue
                # Uncertain delivery/owned composer text requires reconciliation, not reopening.
                protected=any(j['state'] in ('sending','uncertain') or
                    (j['state'] not in ('sent','ignored') and json.loads(j['payload']).get('_owned_draft')) for j in related)
                if protected:continue
                prior=db.execute('SELECT * FROM gap_reviews WHERE request_id=?',(r['id'],)).fetchone()
                attempts=prior['attempts'] if prior else 0
                if observed and (not prior or now-prior['checked']>=30):
                    attempts+=1
                    db.execute('INSERT OR REPLACE INTO gap_reviews VALUES(?,?,?)',(r['id'],attempts,now))
                limit=300 if r['kind']=='chat' else 1800
                if now-r['relevant']>limit or attempts>=2:
                    self._invalidate(db,r['id'],'gap evidence unavailable')
                    db.execute("UPDATE turn_requests SET state='expired',reason='gap evidence unavailable; fresh request required' WHERE id=?",(r['id'],))

    def cached_screen(self, conv, digest, payload=None):
        with self.outbox.connect() as db:
            if payload is not None:
                db.execute('INSERT OR REPLACE INTO turn_screen_cache VALUES(?,?,?)', (conversation_key(conv), digest, encode(payload)))
                return payload
            row = db.execute('SELECT payload FROM turn_screen_cache WHERE conv=? AND digest=?', (conversation_key(conv), digest)).fetchone()
            return json.loads(row['payload']) if row else None

    def audit(self, conv, detail):
        with self.outbox.connect() as db:
            db.execute('INSERT INTO turn_audit(conv,observed,detail) VALUES(?,?,?)',
                       (conversation_key(conv), self.clock(), encode(detail)))

    def ingest(self, conv, messages):
        """Align ordered overlaps, not global text hashes. First/gapped screen is baseline only."""
        messages = clean_messages(messages)
        key, now = conversation_key(conv), self.clock()
        with self.outbox.connect() as db:
            row = db.execute('SELECT messages FROM turn_screens WHERE conv=?', (key,)).fetchone()
            previous = json.loads(row['messages']) if row else []
            old = [x['message'] for x in previous]
            typography = lambda text: ''.join(unicodedata.normalize('NFKC', text).split())
            signature = lambda m: (conversation_key(m['sender']), typography(m['text']), typography(m['quote']), m['self'], m['direct_mention'])
            old_sig, new_sig = list(map(signature, old)), list(map(signature, messages))
            overlap = 0
            for size in range(min(len(old), len(messages)), 0, -1):
                if old_sig[-size:] == new_sig[:size]:
                    overlap = size
                    break
            alignment = 'exact'
            if not overlap and previous:
                # A unique, substantial verbatim suffix may bridge OCR metadata drift.
                # Preserve the already-observed anchor's identity; never turn it into a new event
                # or grant a mention from the conflicting re-read. No fuzzy text/name matching.
                content = lambda m: (typography(m['text']), typography(m['quote']))
                candidates = []
                for size in range(1, min(len(old), len(messages))+1):
                    left, right = old[-size:], messages[:size]
                    if [content(m) for m in left] != [content(m) for m in right]:
                        continue
                    strong = [m for m in left if m['certain'] and m['sender'] and
                              len(m['text'].strip()) >= 6 and not m['text'].startswith('[')]
                    short_texts = {m['text'].strip() for m in left if m['certain'] and m['sender'] and not m['text'].startswith('[')}
                    sequence_anchor = size >= 3 and len(short_texts) >= 2 and sum(map(len, short_texts)) >= 6
                    if strong or sequence_anchor:
                        candidates.append(size)
                if len(candidates) == 1:
                    overlap = candidates[0]
                    alignment = 'verbatim_anchor_metadata_drift'
            offset = 0
            if not overlap and previous:
                # OCR may vary on a cropped paragraph ABOVE unchanged latest messages.
                # Locate a unique exact old suffix inside the new screen; prefix is context only.
                ends = {}
                content = lambda m: (typography(m['text']), typography(m['quote']))
                for size in range(1, min(len(old),len(messages))+1):
                    left=old[-size:]
                    if not any(m['certain'] and m['sender'] and len(m['text'].strip())>=6 and not m['text'].startswith('[') for m in left):
                        continue
                    for pos in range(len(messages)-size+1):
                        if [content(m) for m in left]==[content(m) for m in messages[pos:pos+size]]:
                            ends[pos+size]=(size,pos)
                if len(ends)==1:
                    overlap,offset=next(iter(ends.values()))
                    alignment='unique_suffix_after_cropped_context'
            continuous = bool(previous) and overlap > 0
            # A repeated suffix that has multiple alignments is ambiguous (e.g. three identical "好").
            matches = [i for i in range(len(old)) if old_sig[i:] == new_sig[:len(old)-i] and len(old)-i <= len(messages)]
            if len(matches) > 1:
                continuous = False
            if previous and not continuous:
                # Lost intervening messages: an old candidate can no longer be validated safely.
                for request in db.execute("SELECT id FROM turn_requests WHERE conv=? AND state='open'", (key,)).fetchall():
                    self._invalidate(db, request['id'], 'observation gap')
                    db.execute("UPDATE turn_requests SET state='review',reason='observation gap' WHERE id=?", (request['id'],))
            prefix=[]
            if continuous and offset:
                for message in messages[:offset]:
                    eid=uuid.uuid4().hex
                    prefix.append({'id':eid,'message':message})
                    db.execute('INSERT INTO turn_events VALUES(?,?,?,?)',(eid,key,encode(message),now))
            entries = prefix + previous[-overlap:] if continuous else []
            start = offset+overlap if continuous else 0
            new_ids = []
            for message in messages[start:]:
                eid = uuid.uuid4().hex
                entries.append({'id': eid, 'message': message})
                db.execute('INSERT INTO turn_events VALUES(?,?,?,?)', (eid, key, encode(message), now))
                if continuous:
                    new_ids.append(eid)
            db.execute('INSERT OR REPLACE INTO turn_screens VALUES(?,?,?)', (key, encode(entries), now))
            pending = db.execute('SELECT event_ids FROM turn_reads WHERE conv=? AND planned=0', (key,)).fetchone()
            ids = list(dict.fromkeys((json.loads(pending['event_ids']) if pending and continuous else []) + new_ids))
            db.execute('INSERT OR REPLACE INTO turn_reads VALUES(?,?,0)', (key, encode(ids)))
        self.audit(conv, {'phase': 'ingest', 'continuous': continuous, 'new': len(new_ids), 'baseline': not bool(row), 'alignment': alignment})
        return {'continuous': continuous, 'events': self.events(conv, ids), 'visible': entries}

    def recover_social(self, conv, observation, stamp, max_age=1800):
        """One recent public-chat reaction after a gap; never reconstruct missing requests or @ grants."""
        from datetime import datetime
        try:
            age=self.clock()-datetime.strptime(stamp, '%Y-%m-%d %H:%M').timestamp()
        except (ValueError,TypeError):return observation
        if not 0 <= age <= max_age or not observation['visible']:return observation
        last=observation['visible'][-1];m=last['message']
        if m['self'] or not m['certain'] or not m['sender'] or m['direct_mention']:return observation
        key=conversation_key(conv)
        with self.outbox.connect() as db:
            recent=db.execute("SELECT 1 FROM jobs WHERE conv=? AND state='sent' AND updated>?",(key,self.clock()-120)).fetchone()
            if recent or db.execute("SELECT 1 FROM turn_requests WHERE conv=? AND (state='open' OR (state='review' AND reason='owner review'))",(key,)).fetchone():return observation
            eid=last['id']
            # Message identity, not OCR timestamp, owns recovery. Closed requests stay closed.
            if any(eid in json.loads(r['sources']) for r in db.execute('SELECT sources FROM turn_requests WHERE conv=?',(key,))):return observation
            if db.execute('SELECT 1 FROM turn_recoveries WHERE conv=? AND event_id=? AND planned=1',(key,eid)).fetchone():return observation
            claim=db.execute('SELECT * FROM turn_recovery_events WHERE conv=? AND event_id=?',(key,eid)).fetchone()
            if claim and (claim['done'] or claim['attempts']>=2):return observation
            db.execute('INSERT INTO turn_recovery_events VALUES(?,?,1,0) ON CONFLICT(conv,event_id) DO UPDATE SET attempts=attempts+1',(key,eid))
            row=db.execute('SELECT * FROM turn_recoveries WHERE conv=? AND stamp=?',(key,stamp)).fetchone()
            if row and row['planned']:return observation
            if row:
                old=db.execute('SELECT payload FROM turn_events WHERE id=?',(row['event_id'],)).fetchone()
                if not old or json.loads(old['payload'])!=m:return observation
                eid=row['event_id']
            else:
                eid=last['id']
                db.execute('INSERT INTO turn_recoveries VALUES(?,?,?,0)',(key,stamp,eid))
        self.audit(conv,{'phase':'recent_social_recovery','stamp':stamp,'event_id':eid,'max_age':max_age})
        return dict(observation, recovery_stamp=stamp, events=self.events(conv,[eid]))

    def events(self, conv, ids):
        result = []
        with self.outbox.connect() as db:
            for eid in ids:
                row = db.execute('SELECT payload FROM turn_events WHERE id=? AND conv=?', (eid, conversation_key(conv))).fetchone()
                if row:
                    result.append(dict(json.loads(row['payload']), id=eid))
        return result

    def requests(self, conv):
        with self.outbox.connect() as db:
            rows = db.execute("SELECT * FROM turn_requests WHERE conv=? AND state='open' ORDER BY created", (conversation_key(conv),)).fetchall()
        return [dict(row) for row in rows]

    def planning_requests(self, conv):
        # Waiting for the owner must not hide the original request from later cancellations/corrections.
        with self.outbox.connect() as db:
            rows=db.execute("SELECT * FROM turn_requests WHERE conv=? AND (state='open' OR (state='review' AND reason='owner review')) ORDER BY created",(conversation_key(conv),)).fetchall()
        return [dict(r) for r in rows]

    def apply(self, conv, observation, actions, mention_only, allow_smalltalk=False):
        recovery=observation.get('recovery_stamp')
        if (not observation['continuous'] and not recovery) or not isinstance(actions, list) or len(actions) > 16:
            raise ValueError('unreliable planning observation')
        if recovery and (mention_only or not allow_smalltalk or any(a.get('action') not in ('new','ignore') or (a.get('action')=='new' and (a.get('kind')!='chat' or a.get('join_chat') is not True)) for a in actions)):
            raise ValueError('social recovery cannot authorize requests or commitments')
        events = {e['id']: e for e in observation['events']}
        covered = set()
        for a in actions:
            if isinstance(a, dict) and isinstance(a.get('event_ids'), list):
                covered.update(i for i in a['event_ids'] if isinstance(i, str))
        if covered != set(events):
            raise ValueError('incomplete event plan')
        now, key = self.clock(), conversation_key(conv)
        with self.outbox.connect() as db:
            # All actions validate before commit. A malformed model answer cannot partially mutate turns.
            for a in actions:
                if not isinstance(a, dict) or a.get('action') not in ('new', 'update', 'cancel', 'resolved', 'ignore'):
                    raise ValueError('invalid action')
                ids = a.get('event_ids')
                if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in events for i in ids):
                    raise ValueError('ungrounded action')
                evidence = [events[i] for i in ids]
                if a['action'] == 'ignore':
                    continue
                if any(e['certain'] is not True or not e['sender'].strip() for e in evidence):
                    raise ValueError('uncertain sender or message')
                if not isinstance(a.get('reason'), str):
                    raise ValueError('missing reason')
                rid = a.get('request_id')
                old = db.execute('SELECT * FROM turn_requests WHERE id=? AND conv=? AND (state=\'open\' OR (state=\'review\' AND reason=\'owner review\'))', (rid, key)).fetchone() if isinstance(rid, str) else None
                if a['action'] != 'new' and not old:
                    raise ValueError('unknown request')
                sender = a.get('sender') if a['action'] == 'new' else old['sender']
                own = [e for e in evidence if e['sender'] == sender and not e['self']]
                if not own:
                    raise ValueError('missing requester evidence')
                if a['action'] in ('cancel', 'resolved'):
                    # Resolved requires visible acknowledgement from the requester, not a bystander's joke.
                    self._invalidate(db, rid, a['action'])
                    db.execute('UPDATE turn_requests SET state=?,reason=? WHERE id=?', (a['action'], a['reason'], rid))
                    continue
                if any(e['sender'] != sender or e['self'] for e in evidence):
                    raise ValueError('cannot merge another sender constraints')
                if a.get('kind') == 'chat' and a.get('question') == '':
                    a = dict(a, question='结合当前群聊上下文，简短回应' + sender + '：' + '；'.join(e['text'] for e in own))
                if not isinstance(a.get('question'), str) or not a['question'].strip() or len(a['question']) > 8000:
                    raise ValueError('missing full question')
                if a.get('kind') not in ('answer', 'chat', 'image', 'vision', 'owner'):
                    raise ValueError('invalid request kind')
                if a['kind'] == 'image' and not a.get('image_prompt', '').strip():
                    raise ValueError('image request missing prompt')
                if not isinstance(a.get('context'), str) or not isinstance(a.get('image_prompt'), str):
                    raise ValueError('invalid context')
                auth = [e['id'] for e in own if e['direct_mention'] is True]
                if old:
                    auth = list(dict.fromkeys(json.loads(old['authorization']) + auth))
                if mention_only and not auth:
                    continue
                if a['action'] == 'new':
                    social = (allow_smalltalk is True and not mention_only and a['kind'] == 'chat' and a.get('join_chat') is True)
                    if social and db.execute("SELECT 1 FROM turn_requests WHERE conv=? AND state='open' AND kind='chat' LIMIT 1", (key,)).fetchone():
                        continue
                    if a.get('addressed_to_bot') is not True and not social:
                        continue
                    rid = hashlib.sha256((key + '\0' + sender + '\0' + ids[0]).encode()).hexdigest()
                    priority = 0 if auth else (1 if a['kind'] in ('answer', 'image') else 2)
                    db.execute('INSERT OR IGNORE INTO turn_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (rid, key, conv, sender, a['question'], a['context'], a['kind'], a['image_prompt'], 1, 'open', priority,
                         encode(ids), encode(auth), now, now, now+6, a['reason']))
                else:
                    if not self._invalidate(db, rid, 'revised'):
                        # A possibly sent or typed answer must be reconciled/reviewed, never silently replaced.
                        db.execute('UPDATE turn_requests SET state=\'review\',reason=? WHERE id=?', ('changed with owned draft or uncertain send', rid))
                        continue
                    sources = list(dict.fromkeys(json.loads(old['sources']) + ids))
                    db.execute('UPDATE turn_requests SET question=?,context=?,kind=?,image_prompt=?,revision=revision+1,sources=?,authorization=?,relevant=?,eligible=?,reason=? WHERE id=?',
                        (a['question'], a['context'], a['kind'], a['image_prompt'], encode(sources), encode(auth), now,
                         min(now+6, old['created']+15), 'owner review' if old['state']=='review' and old['reason']=='owner review' else a['reason'], rid))
            db.execute('UPDATE turn_reads SET planned=1 WHERE conv=?', (key,))
            if recovery:
                db.execute('UPDATE turn_recoveries SET planned=1 WHERE conv=? AND stamp=?',(key,recovery))
                for eid in events:db.execute('UPDATE turn_recovery_events SET done=1 WHERE conv=? AND event_id=?',(key,eid))
        self.audit(conv, {'phase': 'plan', 'actions': actions, 'policy': POLICY, 'mention_only': mention_only, 'allow_smalltalk': allow_smalltalk})

    def _invalidate(self, db, rid, reason):
        safe = True
        for row in db.execute("SELECT id,state,payload FROM jobs WHERE state NOT IN ('sent','ignored','human')").fetchall():
            payload = json.loads(row['payload'])
            if payload.get('_request_id') != rid:
                continue
            if row['state'] in ('sending', 'uncertain'):
                safe = False
                continue
            state = 'human' if payload.get('_owned_draft') else 'ignored'
            safe = safe and state != 'human'
            db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=?', (state, reason, self.clock(), row['id']))
        return safe

    def quarantine_legacy(self, conv):
        with self.outbox.connect() as db:
            for row in db.execute("SELECT id,state,payload FROM jobs WHERE conv=? AND state NOT IN ('sent','ignored','human')", (conversation_key(conv),)).fetchall():
                if not json.loads(row['payload']).get('_request_id') and row['state'] not in ('sending', 'uncertain'):
                    db.execute("UPDATE jobs SET state='human',error='turn migration: requester/authorization evidence unavailable' WHERE id=?", (row['id'],))

    def enqueue(self, conv, mention_only):
        now = self.clock()
        requests = sorted(self.requests(conv), key=lambda r: (r['priority'], r['created']))
        for r in requests:
            auth = json.loads(r['authorization'])
            if r['eligible'] > now or (mention_only and not auth):
                continue
            with self.outbox.connect() as db:
                delivery_started = any(json.loads(j['payload']).get('_request_id') == r['id'] for j in db.execute("SELECT payload FROM jobs WHERE conv=? AND state NOT IN ('sent','ignored','human')", (conversation_key(conv),)).fetchall())
            if r['kind'] == 'chat' and now-r['relevant'] > 300 and not delivery_started:
                with self.outbox.connect() as db:
                    db.execute("UPDATE turn_requests SET state='expired' WHERE id=?", (r['id'],))
                continue
            if r['kind'] == 'chat':
                with self.outbox.connect() as db:
                    recent = db.execute("SELECT 1 FROM jobs WHERE conv=? AND state='sent' AND updated>?", (conversation_key(conv), now-30)).fetchone()
                if recent:
                    continue
            payload = {'conversation': conv, 'need_reply': True, 'last_from_customer': True,
                'last_customer_msg': r['question'], 'question_to_answer': r['question'],
                'question_context': r['context'] + '\n提问者：' + r['sender'] + '。仅回答此人此事；自然称呼以明确对象，不输出假@。' + ('这是日常接话，简短自然，不写知识报告。' if r['kind']=='chat' else ''),
                'is_deep_q': r['kind'] == 'answer', '_needs_owner': r['kind'] == 'owner',
                'owner_question': r['context'] if r['kind']=='owner' else '', 'wants_image': r['kind'] == 'image',
                '_needs_turn_draft': r['kind'] in ('chat', 'vision'),
                'needs_image': r['kind'] == 'vision', '_request_sender': r['sender'],
                'image_prompt': r['image_prompt'], 'reply': '',
                'message_event': 'turn:%s:%s' % (r['id'], r['revision']),
                '_request_id': r['id'], '_request_kind': r['kind'], '_request_revision': r['revision'], '_turn_policy': POLICY,
                '_mention_authorized': bool(auth), '_authorization_events': auth, '_priority': r['priority']}
            self.outbox.add(conv, payload)

    def valid(self, job, mention_only, commit=False):
        d = job['payload']
        with self.outbox.connect() as db:
            row = db.execute('SELECT * FROM turn_requests WHERE id=? AND conv=?', (d.get('_request_id'), job['conv'])).fetchone()
            valid = bool(row and row['state'] == 'open' and row['revision'] == d.get('_request_revision')
                         and d.get('_turn_policy') == POLICY and (not mention_only or json.loads(row['authorization'])))
            if valid and commit:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT state,revision FROM turn_requests WHERE id=?', (d.get('_request_id'),)).fetchone()
                if not row or row['state'] != 'open' or row['revision'] != d.get('_request_revision'):
                    return False
                db.execute("UPDATE jobs SET state='sending',payload=?,updated=? WHERE id=? AND state NOT IN ('sent','ignored','human','sending','uncertain')", (encode(d), self.clock(), job['id']))
                valid = db.execute('SELECT changes()').fetchone()[0] == 1
        return valid

    def complete(self, job):
        with self.outbox.connect() as db:
            db.execute("UPDATE turn_requests SET state='sent' WHERE id=? AND revision=?", (job['payload'].get('_request_id'), job['payload'].get('_request_revision')))

    def sync_completed(self, conv):
        # Repair a crash between outbox completion and request completion.
        with self.outbox.connect() as db:
            rows = db.execute("SELECT payload FROM jobs WHERE conv=? AND state='sent'", (conversation_key(conv),)).fetchall()
            for row in rows:
                d = json.loads(row['payload'])
                db.execute("UPDATE turn_requests SET state='sent' WHERE id=? AND revision=?", (d.get('_request_id'), d.get('_request_revision')))
            for row in db.execute("SELECT payload FROM jobs WHERE conv=? AND state='human'", (conversation_key(conv),)).fetchall():
                d = json.loads(row['payload'])
                db.execute("UPDATE turn_requests SET state='review' WHERE id=? AND revision=? AND state='open'", (d.get('_request_id'), d.get('_request_revision')))
