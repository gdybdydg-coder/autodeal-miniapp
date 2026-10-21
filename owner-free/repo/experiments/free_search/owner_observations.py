"""Durable public observations, not a publication oracle or delivery queue.

First appearance, changed add-date and publication are deliberately distinct.
No backend, credentials, paid cache, network or Telegram dependencies.
"""
import math
from pathlib import Path
import sqlite3

from .public_cards import PublicCard

MARKER = 0x41444F31


class Observations:
    def __init__(self, path, capacity=10000):
        path = Path(path)
        if not path.name.endswith('.owner-observations.sqlite3') or path.is_symlink():
            raise ValueError('isolated_observation_path_required')
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('invalid_capacity')
        existed = path.exists()
        self.db = sqlite3.connect(path, isolation_level=None)
        if existed and self.db.execute('PRAGMA application_id').fetchone()[0] != MARKER:
            self.db.close()
            raise ValueError('foreign_database')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(f'''
            PRAGMA application_id={MARKER};
            CREATE TABLE IF NOT EXISTS control (
              id INTEGER PRIMARY KEY CHECK(id=1), baseline REAL, last_observed REAL,
              capacity INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
              id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
              original_add REAL, last_add REAL, date_changed INTEGER NOT NULL,
              baseline INTEGER NOT NULL);
        ''')
        self.db.execute('INSERT OR IGNORE INTO control VALUES(1,NULL,NULL,?)', (capacity,))
        if self.db.execute('SELECT capacity FROM control').fetchone()[0] != capacity:
            self.db.close()
            raise ValueError('capacity_changed')

    def close(self):
        self.db.close()

    def signal(self, listing_id, now, max_age=300):
        """Persistent first-observed evidence; baseline is never a new signal."""
        if (type(now) not in (int, float) or not math.isfinite(now)
                or type(max_age) is not int or not 1 <= max_age <= 900):
            raise ValueError('invalid_signal_clock')
        row = self.db.execute('SELECT first_seen,last_seen,baseline FROM observations WHERE id=?',
                              (listing_id,)).fetchone()
        reason = ('not_observed' if row is None else 'baseline_listing' if row[2]
                  else 'stale_observation' if not 0 <= now-row[0] <= max_age
                  or not 0 <= now-row[1] <= max_age else 'eligible')
        # Once scan coordination exists, callers cannot bypass its gate by passing
        # the raw Observations object to a sender instead of ScanCursor.
        if reason == 'eligible' and self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scans'").fetchone():
            latest = self.db.execute('SELECT s.phase FROM scans s JOIN scan_seen v ON s.id=v.scan_id '
                                     'WHERE v.listing_id=? ORDER BY s.id DESC LIMIT 1',
                                     (listing_id,)).fetchone()
            if latest is None or latest[0] != 'complete':
                reason = 'scan_not_completed'
                # Explicit repeated-head evidence confirms an individual ID,
                # without asserting the changing feed has a reachable tail.
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='head_confirmations'").fetchone():
                    head=self.db.execute('SELECT first_at,last_at,sightings FROM head_confirmations WHERE id=?',(listing_id,)).fetchone()
                    if head and head[2]>=2 and head[0]<head[1] and 0<=now-head[0]<=max_age and 0<=now-head[1]<=max_age:
                        reason='eligible'
        return dict(kind='first_observed', listing_id=listing_id, reason=reason,
                    first_seen=None if row is None else row[0], publication_at=None)

    def ingest(self, cards, observed_at, *, baseline_only=False):
        if (type(observed_at) not in (int, float) or not math.isfinite(observed_at)
                or observed_at <= 0 or type(baseline_only) is not bool or not cards or len(cards) > 200
                or any(not isinstance(c, PublicCard) for c in cards)
                or len({c.listing_id for c in cards}) != len(cards)):
            raise ValueError('invalid_observation')
        result = dict(first_seen=0, previously_seen=0, changed_add_date=0,
                      unknown_add_date=0, source_date_after_baseline=0,
                      publication_proven=0, ready_for_delivery=0,
                      coverage='observed_page_only')
        # A savepoint also composes atomically with a scan cursor transaction.
        self.db.execute('SAVEPOINT observation_ingest')
        try:
            baseline, previous, capacity = self.db.execute(
                'SELECT baseline,last_observed,capacity FROM control').fetchone()
            if previous is not None and observed_at < previous:
                raise ValueError('clock_regression')
            cold = baseline is None
            baseline = observed_at if cold else baseline
            result['cold_start'] = cold
            suppress_new = cold or baseline_only
            result['baseline_only'] = suppress_new
            size = self.db.execute('SELECT count(*) FROM observations').fetchone()[0]
            for card in cards:
                prior = self.db.execute('SELECT last_add FROM observations WHERE id=?',
                                        (card.listing_id,)).fetchone()
                if card.added_at is None:
                    result['unknown_add_date'] += 1
                if prior is None:
                    size += 1
                    if size > capacity:
                        raise ValueError('observation_capacity')
                    self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',
                        (card.listing_id, observed_at, observed_at, card.added_at,
                         card.added_at, 0, int(suppress_new)))
                    result['first_seen'] += 1
                    if (not suppress_new and not card.issues and card.added_at is not None
                            and baseline < card.added_at <= observed_at):
                        result['source_date_after_baseline'] += 1
                else:
                    changed = prior[0] != card.added_at
                    result['previously_seen'] += 1
                    result['changed_add_date'] += int(changed)
                    self.db.execute('UPDATE observations SET last_seen=?,last_add=?, '
                        'date_changed=max(date_changed,?),baseline=max(baseline,?) WHERE id=?',
                        (observed_at, card.added_at, int(changed), int(baseline_only), card.listing_id))
            self.db.execute('UPDATE control SET baseline=?,last_observed=?',
                            (baseline, observed_at))
            self.db.execute('RELEASE observation_ingest')
        except BaseException:
            self.db.execute('ROLLBACK TO observation_ingest')
            self.db.execute('RELEASE observation_ingest')
            raise
        return result
