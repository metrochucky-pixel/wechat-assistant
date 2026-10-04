"""Bounded discovery reuse only. Never caches identity or send authorization."""
import copy
import hashlib
import time
from datetime import date
from bot_reliability import conversation_key
from bot_performance import frame_key


class ScannedRows(list):
    """List recognition evidence is separate from the number of selected rows."""
    def __init__(self, rows, image):
        super().__init__(rows)
        self.frame = frame_key(image)


def verified_list(rows, image):
    return isinstance(rows, ScannedRows) and rows.frame == frame_key(image)


class ListScanCache:
    def __init__(self, clock=time.monotonic, ttl=20):
        self.clock, self.ttl, self.entry = clock, ttl, None

    def key(self, image, patrol, targets):
        return (frame_key(image), bool(patrol), tuple(sorted(conversation_key(c) for c in targets)))

    def get(self, key):
        if self.entry and self.entry[0] == key and self.clock()-self.entry[1] < self.ttl:
            return copy.deepcopy(self.entry[2])
        return None

    def put(self, key, rows):
        self.entry = (key, self.clock(), copy.deepcopy(rows))


class QuietRows:
    def __init__(self, clock=time.monotonic, ttl=120, max_ttl=300):
        self.clock, self.ttl, self.max_ttl, self.entries = clock, ttl, max(ttl,max_ttl), {}

    def fingerprint(self, image, y, pitch=194):
        if y-pitch//2 < 0 or y+pitch//2 > image.height:return None
        # Includes avatar, unread badge, full preview, timestamp, and name.
        row = image.crop((0,y-pitch//2,image.width,y+pitch//2)).convert('RGB')
        return hashlib.sha256(str(image.size).encode()+row.tobytes()).hexdigest()

    def completed(self, conv, fingerprint, action, pending=False):
        key=conversation_key(conv)
        if action != 'skip_无需回复' or pending or not fingerprint:
            self.entries.pop(key, None)
            return
        old=self.entries.get(key)
        interval=min(self.max_ttl,old[2]*2) if old and old[0]==fingerprint else self.ttl
        if len(self.entries)>=200 and key not in self.entries:self.entries.clear()
        self.entries[key]=(fingerprint,self.clock(),interval)

    def skip(self, conv, fingerprint, urgent=False):
        key=conversation_key(conv);old=self.entries.get(key)
        if urgent or not fingerprint or (old and old[0]!=fingerprint):
            self.entries.pop(key,None)
            return False
        # Only a completed inspection extends the interval, never a skipped visit.
        return bool(old and self.clock()-old[1]<old[2])


def priority(row, active, last_attempt):
    key=conversation_key(row.get('name',''))
    return (not row.get('at_me'), not (key in active or row.get('unread') is True), last_attempt.get(key,0))


def count(outbox, kind, conv=''):
    with outbox.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS scheduler_counts (
            day TEXT,kind TEXT,conv TEXT,n INTEGER NOT NULL,
            PRIMARY KEY(day,kind,conv))''')
        db.execute('''INSERT INTO scheduler_counts VALUES(?,?,?,1)
            ON CONFLICT(day,kind,conv) DO UPDATE SET n=n+1''',
            (date.today().isoformat(),kind,conversation_key(conv)))
        db.execute("DELETE FROM scheduler_counts WHERE day < date('now','-30 days')")


class OffscreenPatrol:
    """Bound missing-target discovery; never clicks or grants reply authorization."""
    def __init__(self, clock=time.monotonic):
        self.clock=clock;self.missing={};self.last_search=0

    def choose(self, targets, rows):
        now=self.clock();visible={conversation_key(r.get('name','')) for r in rows}
        missing={conversation_key(name):name for name in targets if conversation_key(name) not in visible}
        self.missing={k:self.missing.get(k,now) for k in missing}
        if any(r.get('at_me') for r in rows) or now-self.last_search<90:return []
        due=[k for k in missing if now-self.missing[k]>=30]
        if not due:return []
        self.last_search=now
        return [missing[k] for k in sorted(due,key=self.missing.get)]


def find_pending_page(image, targets, capture, scan, swipe, same_chrome, max_pages=3, on_mention=None, on_discovery=None):
    """Walk visible list pages only. Returned rows are bound to the returned frame."""
    keys={conversation_key(c) for c in targets};current=image
    for _ in range(max_pages):
        fresh=capture()
        if not same_chrome(image,fresh):return None
        swipe(fresh)
        current=capture()
        if not same_chrome(image,current):return None
        rows=scan(current)
        for row in rows:
            if on_discovery:on_discovery(row)
        mentions=[r for r in rows if r.get('at_me') is True]
        for row in mentions:
            if on_mention:on_mention(row)
        found=[r for r in rows if r.get('at_me') is True or r.get('unread') is True or conversation_key(r.get('name','')) in keys]
        if found:return current,sorted(found,key=lambda r:not r.get('at_me'))
        if frame_key(current)==frame_key(fresh):break
    # Preserve the actual final page; never return coordinates from the starting page.
    return current,[]
