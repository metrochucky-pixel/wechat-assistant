"""Task-wide budgets and capability-preserving provider ordering."""
import contextvars
import time
import signal
from contextlib import contextmanager
from functools import wraps
from bot_reliability import deadline

PROFILES = {
    'navigation': {'total': 35, 'call': 10, 'attempts': 3},
    'delivery': {'total': 75, 'call': 10, 'attempts': 12},
    'visit': {'total': 45, 'call': 10, 'attempts': 5},
    'chat': {'total': 18, 'call': 9, 'attempts': 2},
    'knowledge': {'total': 60, 'call': 22, 'attempts': 3},
    'vision': {'total': 40, 'call': 15, 'attempts': 3},
    'read': {'total': 25, 'call': 12, 'attempts': 2},
    'image_generation': {'total': 65, 'call': 25, 'attempts': 2},
}
_task = contextvars.ContextVar('model_task', default=None)


class BudgetExceeded(Exception): pass


def budgeted(kind):
    def decorate(fn):
        @wraps(fn)
        def run(*args,**kwargs):
            with task(kind):return fn(*args,**kwargs)
        return run
    return decorate


@contextmanager
def task(kind):
    if _task.get() is not None:
        yield
        return
    profile=PROFILES[kind]
    state=dict(profile, kind=kind, end=time.monotonic()+profile['total'], calls=0)
    state['visit_deadline']=time.monotonic()+min(120,signal.getitimer(signal.ITIMER_REAL)[0] or 120) if kind=='visit' else state['end']
    token=_task.set(state)
    try:
        with deadline(profile['total']):yield
    finally:_task.reset(token)


def begin_delivery():
    """One-way phase transition; repeat calls cannot replenish send/receipt budgets."""
    state=_task.get()
    if not state or state['kind']!='visit':return
    if time.monotonic()>=state['end']:raise BudgetExceeded('observation deadline exhausted')
    profile=PROFILES['delivery'];remaining=min(profile['total'],state['visit_deadline']-time.monotonic())
    if remaining<=0:raise BudgetExceeded('visit deadline exhausted')
    state.update(profile,kind='delivery',calls=0,end=time.monotonic()+remaining)
    # task(visit) owns this main-thread timer. The enclosing hard visit limit is 120s.
    signal.setitimer(signal.ITIMER_REAL,remaining)


def timeout(requested=20):
    state=_task.get()
    if state is None:return min(float(requested),20)
    remaining=state['end']-time.monotonic()
    if remaining<=.1 or state['calls']>=state['attempts']:raise BudgetExceeded('task budget exhausted')
    state['calls']+=1
    return min(float(requested),state['call'],remaining)


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS model_health (
        task TEXT, provider TEXT, latency REAL, failures INTEGER, updated REAL,
        PRIMARY KEY(task,provider))''')


def record(outbox, provider, seconds, success):
    kind=(_task.get() or {}).get('kind','read')
    with outbox.connect() as db:
        _schema(db)
        old=db.execute('SELECT * FROM model_health WHERE task=? AND provider=?',(kind,provider)).fetchone()
        latency=seconds if not old else .7*old['latency']+.3*seconds
        failures=0 if success else min(5,(old['failures'] if old else 0)+1)
        db.execute('INSERT OR REPLACE INTO model_health VALUES(?,?,?,?,?)',(kind,provider,latency,failures,time.time()))


def order(outbox, kind, providers):
    # Candidates are provided by the caller: web search never falls back to
    # an ordinary text model and visual jobs never use a text-only provider.
    with outbox.connect() as db:
        _schema(db)
        recent={r['provider']:r for r in db.execute('SELECT * FROM model_health WHERE task=? AND updated>?',(kind,time.time()-900))}
    def score(name):
        row=recent.get(name)
        return row['latency']+row['failures']*15 if row else 5+providers.index(name)*2
    return sorted((p for p in providers if outbox.provider_allowed(p)),key=score)
