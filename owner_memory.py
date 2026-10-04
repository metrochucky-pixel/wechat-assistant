"""Conservative owner-origin learning. No models, phone access or identity inference."""
import hashlib,json,re,time
from datetime import datetime
from difflib import SequenceMatcher
import natural_preferences


def norm(text):
    return ''.join(c for c in str(text).casefold() if c.isalnum())


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS owner_samples(event_id TEXT PRIMARY KEY,conv TEXT,text TEXT,observed REAL,checked REAL,origin TEXT);
    CREATE TABLE IF NOT EXISTS owner_avoid_evidence(word TEXT,source TEXT,evidence TEXT,updated REAL,PRIMARY KEY(word,source,evidence));
    CREATE TABLE IF NOT EXISTS owner_daily(day TEXT PRIMARY KEY,n INTEGER,short INTEGER,emoji INTEGER);
    CREATE TABLE IF NOT EXISTS owner_preferences(key TEXT PRIMARY KEY,value TEXT,source TEXT,evidence TEXT,updated REAL,active INTEGER);
    ''')


def similar(a,b):
    a,b=norm(a),norm(b)
    if not a or not b:return False
    return a==b or (min(len(a),len(b))>=8 and SequenceMatcher(None,a,b).ratio()>=.85)


def capture(outbox, conv, observation):
    """Two separate consistent readings, new continuous events, and bot-write exclusion.
    Even accepted records are 'owner_likely', never proof of a personal fact.
    """
    now=time.time()
    with outbox.connect() as db:
        init(db)
        jobs=list(db.execute('SELECT state,payload,created,updated FROM jobs WHERE conv=?',(conv,)))
        bot_text=[];ambiguous=False
        for job in jobs:
            payload=json.loads(job['payload'])
            bot_text.extend((payload.get(k,''),job['created'],job['updated']) for k in ('reply','_owned_draft'))
            ambiguous |= job['state'] in ('sending','uncertain','prepared')
        def matched(text,observed):return any(t and created-10<=observed<=updated+120 and similar(text,t) for t,created,updated in bot_text)
        # A late send receipt or OCR variant can invalidate a previous candidate.
        for r in db.execute("SELECT * FROM owner_samples WHERE conv=? AND origin!='excluded'",(conv,)).fetchall():
            if matched(r['text'],r['observed']):db.execute("UPDATE owner_samples SET origin='excluded' WHERE event_id=?",(r['event_id'],))
        if not observation.get('continuous'):return
        natural_preferences.bind_context(db,conv,observation,now)
        for event in observation.get('events',[]):
            text=event.get('text','')
            if event.get('self') is not True or event.get('certain') is not True or not 2<=len(text)<=1000 or event.get('quote') or '[' in text or 'http' in text:continue
            db.execute('INSERT OR IGNORE INTO owner_samples VALUES(?,?,?,?,?,?)',
                (event['id'],conv,text,now,now,'excluded' if matched(text,now) else 'pending_origin' if ambiguous else 'candidate'))
        for entry in observation.get('visible',[]):
            m=entry['message'];r=db.execute('SELECT * FROM owner_samples WHERE event_id=? AND conv=?',(entry['id'],conv)).fetchone()
            if r and r['origin'] in ('candidate','pending_origin') and now-r['observed']>=10 and m.get('self') is True and m.get('certain') is True and m.get('text')==r['text'] and not ambiguous and not matched(r['text'],r['observed']):
                db.execute("UPDATE owner_samples SET origin='owner_likely',checked=? WHERE event_id=?",(now,r['event_id']))


def set_preference(outbox,key,value,source,evidence):
    if source!='owner_explicit':raise ValueError('explicit owner source required')
    if key not in ('brevity','emoji','avoid_words'):raise ValueError('unsupported preference')
    if key=='brevity' and value not in ('short','detailed'):raise ValueError('invalid brevity')
    if key=='emoji' and value not in ('less','natural'):raise ValueError('invalid emoji')
    if key=='avoid_words' and (not isinstance(value,list) or not value or any(not isinstance(w,str) or not 1<=len(w)<=12 or not re.fullmatch(r'[\w]+',w) for w in value)):raise ValueError('invalid words')
    with outbox.connect() as db:
        init(db)
        db.execute('INSERT OR REPLACE INTO owner_preferences VALUES(?,?,?,?,?,1)',(key,json.dumps(value,ensure_ascii=False),source,evidence,time.time()))


def correction(text):
    # Only explicit bot-directed, whole-message presentation corrections.
    text=text.strip().rstrip('。！!')
    match=re.fullmatch(r'(?:bot|机器人|分身)[，,:：\s]*(?:以后)?(?:回复|说话)?(简短一点|短一点|详细一点|少用表情|不要表情)',text,re.I)
    if match:
        return {'简短一点':('brevity','short'),'短一点':('brevity','short'),'详细一点':('brevity','detailed'),
                '少用表情':('emoji','less'),'不要表情':('emoji','less')}[match[1]]
    match=re.fullmatch(r'(?:bot|机器人|分身)[，,:：\s]*(?:以后)?(?:别再说|不要再说)([\w]{1,12})',text,re.I)
    return ('avoid_words',[match[1]]) if match else None


def summarize(outbox):
    """Daily aggregates from likely owner samples; no free-form model rules."""
    now=time.time()
    with outbox.connect() as db:
        init(db)
        rows=list(db.execute("SELECT * FROM owner_samples WHERE origin='owner_likely' AND observed>?",(now-90*86400,)))
        days={}
        for r in rows:
            day=datetime.fromtimestamp(r['observed']).strftime('%Y-%m-%d');n,short,emoji=days.get(day,(0,0,0))
            days[day]=(n+1,short+int(len(r['text'])<=40),emoji+int(any(ord(c)>0x1ffff for c in r['text'])))
        # Rebuild so later bot-origin exclusion also removes derived evidence.
        db.execute('DELETE FROM owner_daily')
        db.executemany('INSERT INTO owner_daily VALUES(?,?,?,?)',[(day,*v) for day,v in days.items()])
        db.execute("DELETE FROM owner_avoid_evidence WHERE source='observed_correction'")
        # Recompute automatic corrections with their source event; revocation removes them.
        db.execute("UPDATE owner_preferences SET active=0 WHERE source='observed_correction'")
        for r in sorted(rows,key=lambda r:r['observed']):
            item=correction(r['text'])
            if not item:continue
            key,value=item
            # Every observed scalar correction belongs to the scoped candidate path.
            # Unsupported wording must never fall back to a global preference.
            if key!='avoid_words':continue
            old=db.execute('SELECT source,active FROM owner_preferences WHERE key=?',(key,)).fetchone()
            if key=='avoid_words':
                if old and old['source']=='owner_explicit' and not old['active']:continue
                for word in value:db.execute('INSERT OR REPLACE INTO owner_avoid_evidence VALUES(?,?,?,?)',(word,'observed_correction',r['event_id'],r['observed']))
                continue
        # Only infer a small presentation preference after repetition across days.
        n=sum(v[0] for v in days.values());short=sum(v[1] for v in days.values())
        row=db.execute("SELECT source,active FROM owner_preferences WHERE key='brevity'").fetchone()
        if not row or row['source']=='repeated_style' or (not row['active'] and row['source']!='owner_explicit'):
            if n>=10 and len(days)>=3 and short/n>=.8:
                db.execute("INSERT OR REPLACE INTO owner_preferences VALUES('brevity',?,'repeated_style',?,?,1)",
                    (json.dumps('short'),json.dumps({'days':len(days),'samples':n}),now))
            else:db.execute("UPDATE owner_preferences SET active=0 WHERE key='brevity' AND source='repeated_style'")
        natural_preferences.refresh(db,now)
        db.execute('DELETE FROM owner_samples WHERE observed<?',(now-90*86400,))
        db.execute('DELETE FROM owner_samples WHERE event_id IN (SELECT event_id FROM owner_samples ORDER BY observed DESC LIMIT -1 OFFSET 2000)')


def render(outbox):
    with outbox.connect() as db:
        init(db);rows=list(db.execute("SELECT * FROM owner_preferences WHERE active=1 AND source!='observed_correction' ORDER BY key"))
        stop=db.execute("SELECT 1 FROM owner_preferences WHERE key='avoid_words' AND source='owner_explicit' AND active=0").fetchone()
        automatic=[] if stop else [r[0] for r in db.execute('''SELECT DISTINCT e.word FROM owner_avoid_evidence e
            JOIN owner_samples s ON s.event_id=e.evidence
            WHERE e.source='observed_correction' AND s.conv=? AND s.origin='owner_likely' AND s.observed>?
            ORDER BY e.word''',(natural_preferences._current.get(),time.time()-90*86400))]
        group_preferences=natural_preferences.active(db,natural_preferences._current.get())
    lines=[]
    for r in rows:
        if r['source']!='owner_explicit' and r['key'] in group_preferences:continue
        value=json.loads(r['value'])
        if r['key']=='brevity':line={'short':'日常接话优先简短自然；复杂问题保留必要信息。','detailed':'需要解释的问题可多讲一点，避免空话。'}.get(value)
        elif r['key']=='emoji':line={'less':'少用表情符号。','natural':'表情自然使用，不强行添加。'}.get(value)
        elif r['key']=='avoid_words':line='避免把这些词当作称呼或口头禅：'+'、'.join(value)
        else:line=None
        if line:lines.append(({'owner_explicit':'机主明确偏好：','observed_correction':'观察到的对bot的措辞纠正（服从人工风格规则）：'}.get(r['source'],'反复观察到的表达倾向（服从人工风格规则）：'))+line)
    manual={word for r in rows if r['key']=='avoid_words' for word in json.loads(r['value'])}
    extra=[word for word in automatic if word not in manual]
    if extra:lines.append('观察到的措辞纠正：避免把这些词当口头禅：'+'、'.join(extra))
    if group_preferences:lines.append('当前会话观察到的表达纠正（服从明确人工风格规则）：'+ '\n'.join(group_preferences.values()))
    return '\n'.join(lines)


def status(outbox):
    with outbox.connect() as db:
        init(db);natural_preferences.init(db)
        return {'feedback_candidates':[dict(r) for r in db.execute('SELECT id,conv,key,value,status,reason,bot_job,decision FROM style_candidates ORDER BY observed DESC LIMIT 30')],
                'origins':dict(db.execute('SELECT origin,count(*) FROM owner_samples GROUP BY origin')),
                'word_evidence':[dict(r) for r in db.execute('SELECT word,source,evidence,updated FROM owner_avoid_evidence ORDER BY word')],
                'days':db.execute('SELECT count(*) FROM owner_daily').fetchone()[0],
                'preferences':[dict(r) for r in db.execute('SELECT key,value,source,updated,active FROM owner_preferences')],
                'limits':'本人来源为保守推断；只学表达统计，不自动推断身份、关系、经历或权限。'}


def start(outbox):
    import threading
    def run():
        while True:
            try:summarize(outbox)
            except Exception as exc:print('[owner memory] '+type(exc).__name__)
            time.sleep(60)
    threading.Thread(target=run,name='owner-memory',daemon=True).start()
