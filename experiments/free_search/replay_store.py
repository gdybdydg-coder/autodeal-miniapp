"""Isolated durable fixture replay. No environment configuration or network I/O."""
import json
from pathlib import Path
import sqlite3

from experiments.free_search.offline_pipeline import PipelineState

APPLICATION_ID = 0x46524545


class ReplayStore:
    """A single-writer transactional state plus diagnostic event journal.

    Explicit experiment-only filenames and an application marker prevent
    accidentally opening a production SQLite database. No DATABASE_URL is read.
    """

    def __init__(self, path):
        path = Path(path)
        if not path.name.endswith('.free-search.sqlite3') or path.is_symlink():
            raise ValueError('experiment_database_required')
        existed = path.exists()
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        marker = self.db.execute('PRAGMA application_id').fetchone()[0]
        if existed and marker != APPLICATION_ID:
            self.db.close()
            raise ValueError('foreign_database')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL)')
            self.db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps(PipelineState().as_dict()),))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def load(self):
        return PipelineState.from_dict(json.loads(self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]))

    def apply(self, at, stage, action):
        """Commit progress only together with accepted work and its diagnostic.

        action(state) returns (new_state, stable_reason). The action must be
        pure; simulated send claims and send outcomes are separate transactions.
        """
        import math
        import re
        if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
            raise ValueError('invalid_time')
        if not re.fullmatch(r'[a-z_]{1,40}', stage):
            raise ValueError('invalid_stage')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            state, reason = action(self.load())
            payload = json.dumps(PipelineState.from_dict(state.as_dict()).as_dict(), allow_nan=False)
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z_]{1,80}', reason):
                raise ValueError('invalid_reason')
            self.db.execute('UPDATE state SET payload=? WHERE id=1', (payload,))
            self.db.execute('INSERT INTO events(at,stage,reason) VALUES (?,?,?)', (at, stage, reason))
            self.db.commit()
            return state
        except BaseException:
            self.db.rollback()
            raise

    def diagnostics(self):
        return [{'stage': stage, 'reason': reason, 'count': count} for stage, reason, count in
                self.db.execute('SELECT stage,reason,count(*) FROM events GROUP BY stage,reason ORDER BY stage,reason')]
