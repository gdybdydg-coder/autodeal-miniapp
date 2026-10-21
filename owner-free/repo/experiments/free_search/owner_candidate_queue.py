"""Durable local work stages, not a production delivery queue or send authority.

No lease expiry can requeue a claimed/uncertain delivery. The shared production
delivery constraint remains mandatory for the separate one-shot sender.
"""
import json
import math
import re
import sqlite3

NEXT = {'discovered': {'detail', 'rejected'}, 'detail': {'range', 'rejected'},
        'range': {'qualified', 'rejected'}, 'qualified': {'claimed', 'rejected'},
        'claimed': {'sent', 'uncertain', 'cancelled'}}
TERMINAL = {'rejected', 'sent', 'uncertain', 'cancelled'}


class CandidateQueue:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS candidates ('
                        'id TEXT PRIMARY KEY,url TEXT NOT NULL,first_seen REAL NOT NULL,'
                        'stage TEXT NOT NULL,updated REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS stage_events ('
                        'id INTEGER PRIMARY KEY,candidate TEXT NOT NULL,stage TEXT NOT NULL,'
                        'at REAL NOT NULL,evidence TEXT NOT NULL)')

    def close(self):
        self.db.close()

    @staticmethod
    def timestamp(now):
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ValueError('invalid_stage_timestamp')

    def discover(self, ident, url, first_seen):
        self.timestamp(first_seen)
        if (not isinstance(ident,str) or not re.fullmatch(r'[1-9][0-9]{0,11}',ident)
                or not isinstance(url,str)
                or not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                    + ident + r'\.html',url)):
            raise ValueError('invalid_candidate_identity')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT url FROM candidates WHERE id=?',(ident,)).fetchone()
            if old:
                if old[0]!=url: raise ValueError('candidate_identity_conflict')
                self.db.execute('COMMIT')
                return False
            self.db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)',
                            (ident,url,first_seen,'discovered',first_seen))
            self.db.execute('INSERT INTO stage_events(candidate,stage,at,evidence) VALUES(?,?,?,?)',
                            (ident,'discovered',first_seen,'{}'))
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def advance(self, ident, expected, stage, now, evidence):
        self.timestamp(now)
        if stage not in NEXT.get(expected,set()): raise ValueError('invalid_stage_transition')
        if not isinstance(evidence,dict) or not evidence:
            raise ValueError('stage_evidence_required')
        encoded=json.dumps(evidence,sort_keys=True,allow_nan=False)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed=self.db.execute('UPDATE candidates SET stage=?,updated=? '
                'WHERE id=? AND stage=? AND updated<=?',(stage,now,ident,expected,now)).rowcount
            if changed!=1: raise ValueError('stage_conflict_or_clock_regression')
            self.db.execute('INSERT INTO stage_events(candidate,stage,at,evidence) VALUES(?,?,?,?)',
                            (ident,stage,now,encoded))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def pending(self, limit=20, *, stage=None):
        if type(limit) is not int or not 1<=limit<=100: raise ValueError('invalid_batch_size')
        if stage is not None and stage not in ('discovered','detail','range','qualified'):
            raise ValueError('invalid_pending_stage')
        # Claimed is deliberately excluded: a crash must not authorize retry.
        rows=self.db.execute("SELECT id,url,first_seen,stage,updated FROM candidates "
            "WHERE stage IN ('discovered','detail','range','qualified') AND (? IS NULL OR stage=?) ORDER BY first_seen,id LIMIT ?",
            (stage,stage,limit)).fetchall()
        return [dict(zip(('id','url','first_seen','stage','updated'),r)) for r in rows]

    def get(self, ident):
        row=self.db.execute('SELECT id,url,first_seen,stage,updated FROM candidates WHERE id=?',(ident,)).fetchone()
        return None if row is None else dict(zip(('id','url','first_seen','stage','updated'),row))

    def evidence(self, ident, stage):
        row=self.db.execute('SELECT evidence FROM stage_events WHERE candidate=? AND stage=? ORDER BY id DESC LIMIT 1',
                            (ident,stage)).fetchone()
        return None if row is None else json.loads(row[0])

    def durations(self):
        # Observed wall-clock stage intervals; NOT publication latency.
        rows=self.db.execute('SELECT candidate,stage,at FROM stage_events ORDER BY candidate,id').fetchall()
        previous={}; result={}
        for ident,stage,at in rows:
            if ident in previous:
                before,start=previous[ident]
                result.setdefault(before+'->'+stage,[]).append(at-start)
            previous[ident]=(stage,at)
        return result
