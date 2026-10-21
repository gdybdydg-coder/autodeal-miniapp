"""New explicit daytime session; preserves expired night rules and shared lock."""
import math,re,time,uuid
from contextlib import contextmanager
from .owner_night_runtime import Coordinator

@contextmanager
def session(path,seconds,*,clock=time.time):
    if type(seconds) is not int or not 1<=seconds<=2400:raise ValueError('invalid_session_duration')
    now=clock()
    if not math.isfinite(now) or now<=0:raise ValueError('invalid_session_clock')
    coordinator=Coordinator(path);token='comparison_'+uuid.uuid4().hex;acquired=False
    try:
        coordinator.db.execute('BEGIN IMMEDIATE')
        try:
            if coordinator.db.execute('SELECT 1 FROM executor').fetchone():raise ValueError('executor_busy')
            coordinator.db.execute('INSERT INTO executor VALUES(1,?,?)',(token,now))
            coordinator.db.execute('COMMIT');acquired=True
        except BaseException:
            coordinator.db.execute('ROLLBACK');raise
        yield now+seconds
    finally:
        if acquired:coordinator.release(token)
        coordinator.close()
