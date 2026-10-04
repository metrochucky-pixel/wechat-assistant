"""Reuse facts only after current original-image proof, never a draft or send permission."""
import hashlib
import json
import time

VERSION = 1
TTL = 86400


def key(frames, zoom, request, scope):
    h = hashlib.sha256(json.dumps([VERSION, scope, request], ensure_ascii=False, sort_keys=True).encode())
    for image in list(frames) + ([zoom] if zoom is not None else []):
        # Exclude only Android clock/status bar. All content and viewer controls remain.
        im = image.convert('RGB'); im = im.crop((0, int(im.height*.045), im.width, im.height))
        h.update(str(im.size).encode()); h.update(im.tobytes())
    return h.hexdigest()


def valid(value):
    return isinstance(value, dict) and all(isinstance(value.get(k), str) for k in ('observed','text','uncertainty')) and bool(value['observed'].strip())


def analyze(outbox, read, frames, request, zoom=None, scope=''):
    from image_review import facts
    stamp = key(frames, zoom, request, scope)
    with outbox.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS image_fact_cache(key TEXT PRIMARY KEY,facts TEXT,created REAL,hits INTEGER DEFAULT 0)')
        db.execute('DELETE FROM image_fact_cache WHERE created<?', (time.time()-TTL,))
        row = db.execute('SELECT facts FROM image_fact_cache WHERE key=?', (stamp,)).fetchone()
        if row:
            value = json.loads(row['facts'])
            if valid(value):
                db.execute('UPDATE image_fact_cache SET hits=hits+1 WHERE key=?', (stamp,))
                return value, True
    value = facts(read, frames, request, zoom)
    if not valid(value):
        return None, False
    with outbox.connect() as db:
        db.execute('INSERT OR REPLACE INTO image_fact_cache(key,facts,created) VALUES(?,?,?)', (stamp,json.dumps(value,ensure_ascii=False),time.time()))
        db.execute('DELETE FROM image_fact_cache WHERE key IN (SELECT key FROM image_fact_cache ORDER BY created DESC LIMIT -1 OFFSET 200)')
    return value, False
