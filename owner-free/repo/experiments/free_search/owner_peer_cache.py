"""Local normalized public evidence only; no credentials, network or sender."""
import json
import math
from pathlib import Path
import re
import sqlite3

FIELDS = frozenset({'listing_id', 'brand', 'model', 'year', 'fuel', 'transmission',
    'mileage_km', 'price', 'currency', 'body', 'generation', 'engine_cc', 'region',
    'observed_at', 'drive'})
MARKER = 0x41445043


def valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class PeerCache:
    def __init__(self, path, capacity=10000):
        path = Path(path)
        if not path.name.endswith('.owner-peers.sqlite3') or path.is_symlink():
            raise ValueError('isolated_peer_cache_required')
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('invalid_capacity')
        exists = path.exists()
        self.db = sqlite3.connect(path, isolation_level=None)
        if exists and self.db.execute('PRAGMA application_id').fetchone()[0] != MARKER:
            self.db.close()
            raise ValueError('foreign_database')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(f'''PRAGMA application_id={MARKER};
          CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY CHECK(id=1),capacity INTEGER);
          CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, observed REAL NOT NULL,
            state TEXT NOT NULL, reason TEXT, payload TEXT);
        ''')
        self.db.execute('INSERT OR IGNORE INTO policy VALUES(1,?)', (capacity,))
        if self.db.execute('SELECT capacity FROM policy').fetchone()[0] != capacity:
            self.db.close()
            raise ValueError('capacity_changed')

    def close(self):
        self.db.close()

    def put(self, ident, observed, *, row=None, reason=None):
        if not isinstance(ident, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', ident) or not valid_time(observed):
            raise ValueError('invalid_identity_or_time')
        if (row is None) == (reason is None):
            raise ValueError('exactly_one_outcome_required')
        if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r'[a-z_]{1,100}', reason)):
            raise ValueError('invalid_reason')
        if row is not None and (set(row)-FIELDS or row.get('listing_id') != ident
                                or row.get('observed_at') != observed):
            raise ValueError('invalid_public_record')
        payload = None if row is None else json.dumps(row, sort_keys=True, allow_nan=False, default=str)
        if payload is not None and len(payload) > 5000:
            raise ValueError('record_oversize')
        state = 'parsed' if row is not None else 'unresolved'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior = self.db.execute('SELECT observed,state,reason,payload FROM evidence WHERE id=?', (ident,)).fetchone()
            value = (observed, state, reason, payload)
            if prior:
                if observed < prior[0]:
                    raise ValueError('stale_write')
                if observed == prior[0] and prior != value:
                    raise ValueError('same_time_conflict')
            elif self.db.execute('SELECT count(*) FROM evidence').fetchone()[0] >= self.db.execute('SELECT capacity FROM policy').fetchone()[0]:
                raise ValueError('cache_capacity')
            self.db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?)', (ident, *value))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def snapshot(self, now, max_age=86400):
        if not valid_time(now) or not valid_time(max_age) or max_age > 86400:
            raise ValueError('invalid_freshness_policy')
        rows, unavailable = [], {}
        for ident, observed, state, reason, payload in self.db.execute('SELECT * FROM evidence ORDER BY id'):
            if observed > now:
                unavailable[ident] = 'future_observation'
            elif now-observed > max_age:
                unavailable[ident] = 'stale_public_record'
            elif state != 'parsed':
                unavailable[ident] = reason
            else:
                rows.append(json.loads(payload))
        return dict(rows=rows, unavailable=unavailable, publication_proven=False,
                    ready_for_delivery=False, max_age_seconds=max_age)
