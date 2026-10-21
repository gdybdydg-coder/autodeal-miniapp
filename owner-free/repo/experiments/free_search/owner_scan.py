"""Durable bounded scan cursor; no network, scheduler or delivery activation.

All initial pages remain baseline, including after interruption. A declared end
and unchanged head bracket permit completing this local scan, NOT proving an
atomic source snapshot or whole-market recall. Source scope changes fail closed.
"""
from contextlib import contextmanager
import json
import math
import re

from .public_pagination import PageMetadata

HEAD = 'https://auto.ria.com/uk/last/hour/'


class ScanCursor:
    def __init__(self, observations):
        self.observations = observations
        self.db = observations.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS scan_control (
              id INTEGER PRIMARY KEY CHECK(id=1), initial_complete INTEGER NOT NULL,
              established_scope TEXT);
            INSERT OR IGNORE INTO scan_control VALUES(1,0,NULL);
            CREATE TABLE IF NOT EXISTS scans (
              id INTEGER PRIMARY KEY, phase TEXT NOT NULL, baseline INTEGER NOT NULL,
              expected TEXT, scope TEXT, size INTEGER, page_index INTEGER NOT NULL,
              head_ids TEXT, started REAL NOT NULL, last_at REAL NOT NULL, reason TEXT);
            CREATE TABLE IF NOT EXISTS scan_seen (
              scan_id INTEGER NOT NULL, listing_id TEXT NOT NULL,
              PRIMARY KEY(scan_id,listing_id));
            CREATE TABLE IF NOT EXISTS scan_recovery (
              parent_id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL UNIQUE,
              root_id INTEGER NOT NULL, recovered_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS scan_scope_transition (
              parent_id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL UNIQUE,
              old_scope TEXT NOT NULL, new_scope TEXT NOT NULL,
              evidence_sha256 TEXT NOT NULL, transitioned_at REAL NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(scans)')}
        if 'head_url' not in columns:
            self.db.execute('ALTER TABLE scans ADD COLUMN head_url TEXT')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def status(self):
        cursor = self.db.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description], row)) if row else None

    def start(self, now, *, head_url=HEAD):
        if head_url not in (HEAD, HEAD+'?page=1'):
            raise ValueError('unsupported_scan_head')
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ValueError('invalid_scan_clock')
        with self.transaction():
            old = self.status()
            if old and old['phase'] not in ('complete', 'abandoned'):
                raise ValueError('scan_requires_resume_or_explicit_abandon')
            if old and now < old['last_at']:
                raise ValueError('clock_regression')
            if self.db.execute('SELECT count(*) FROM scans').fetchone()[0] >= 100:
                raise ValueError('scan_capacity')
            complete = self.db.execute('SELECT initial_complete FROM scan_control').fetchone()[0]
            self.db.execute('INSERT INTO scans(phase,baseline,expected,page_index,started,last_at,head_url) '
                            'VALUES(?,?,?,0,?,?,?)', ('pages', int(not complete), head_url, now, now, head_url))
        return self.status()

    def abandon(self):
        # Explicit bounded recovery from HEAD; identities and baseline flags survive.
        with self.transaction():
            row = self.status()
            if not row or row['phase'] in ('complete', 'abandoned'):
                raise ValueError('no_unfinished_scan')
            self.db.execute("UPDATE scans SET phase='abandoned' WHERE id=?", (row['id'],))

    def block_source(self):
        """Persist a failed source read; resuming must not silently retry it."""
        with self.transaction():
            row=self.status()
            if row and row['phase'] in ('pages','head_check'):
                self.db.execute("UPDATE scans SET phase='blocked',reason='source_unavailable_no_retry' WHERE id=?",(row['id'],))
        return self.status()

    def recover_moving_head(self, now):
        """Explicit bounded restart; never clears identities or declares coverage.

        At most two recovery scans per chain, even after process restart. Only a
        changed head qualifies; scope/size/source failures require separate review.
        Old scan evidence and first_seen timestamps survive the atomic transition.
        """
        return self._recover(now, 'head_changed_recovery_required')

    def recover_missing_page(self, now, receipt):
        """A confirmed 404 on an advertised later page permits a bounded restart.

        It never counts as end-of-feed and shares the moving-head retry limit.
        Authentication, rate limits and other source failures are ineligible.
        """
        row=self.status()
        if (not row or row['phase']!='blocked' or row['reason']!='source_unavailable_no_retry'
                or row['page_index']<=0 or not isinstance(receipt,dict)
                or receipt.get('status')!=404 or receipt.get('url')!=row['expected']
                or receipt.get('error')!='http_error'
                or type(receipt.get('completed_at')) not in (int,float)
                or not row['last_at']<=receipt['completed_at']<=now):
            raise ValueError('missing_page_recovery_evidence_required')
        return self._recover(now,'source_unavailable_no_retry')

    def _recover(self, now, required_reason):
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ValueError('invalid_scan_clock')
        with self.transaction():
            row = self.status()
            if not row or row['phase'] != 'blocked' or row['reason'] != required_reason:
                raise ValueError('moving_head_recovery_not_applicable')
            if now < row['last_at']:
                raise ValueError('clock_regression')
            prior = self.db.execute('SELECT root_id FROM scan_recovery WHERE child_id=?',
                                    (row['id'],)).fetchone()
            root_id = prior[0] if prior else row['id']
            count = self.db.execute('SELECT count(*) FROM scan_recovery WHERE root_id=?',
                                    (root_id,)).fetchone()[0]
            if count >= 2:
                raise ValueError('moving_head_recovery_limit')
            if self.db.execute('SELECT count(*) FROM scans').fetchone()[0] >= 100:
                raise ValueError('scan_capacity')
            head = row['head_url'] or HEAD
            self.db.execute("UPDATE scans SET phase='abandoned' WHERE id=?", (row['id'],))
            cursor = self.db.execute(
                'INSERT INTO scans(phase,baseline,expected,page_index,started,last_at,head_url,scope,size) '
                'VALUES(?,?,?,0,?,?,?,?,?)',
                ('pages',row['baseline'],head,now,now,head,row['scope'],row['size']))
            self.db.execute('INSERT INTO scan_recovery VALUES(?,?,?,?)',
                            (row['id'],cursor.lastrowid,root_id,now))
        return self.status()

    def accept(self, metadata, cards, now):
        """Commit observations and cursor together; rejected page never advances."""
        with self.transaction():
            row = self.status()
            if not row or row['phase'] not in ('pages', 'head_check'):
                raise ValueError('scan_not_accepting_pages')
            if type(now) not in (int, float) or not math.isfinite(now) or now < row['last_at']:
                raise ValueError('clock_regression')
            reason = None
            established = self.db.execute('SELECT established_scope FROM scan_control').fetchone()[0]
            if not isinstance(metadata, PageMetadata) or metadata.issues:
                reason = 'invalid_page_metadata'
            elif metadata.url != row['expected'] or metadata.page_index != row['page_index']:
                reason = 'unexpected_page'
            elif not metadata.scope_digest or not metadata.page_size:
                reason = 'missing_scope_or_size'
            elif ((row['scope'] and metadata.scope_digest != row['scope']) or
                  (established and metadata.scope_digest != established)):
                reason = 'scope_metadata_changed'
            elif row['size'] and metadata.page_size != row['size']:
                reason = 'page_size_changed'
            elif len(cards) > metadata.page_size:
                reason = 'page_cardinality_conflict'
            elif not metadata.terminal_declared and not metadata.next_url:
                reason = 'next_or_end_missing'
            if reason:
                self.db.execute("UPDATE scans SET phase='blocked',reason=?,last_at=? WHERE id=?",
                                (reason, now, row['id']))
            else:
                # Validation and capacity errors roll back both cursor and observations.
                self.observations.ingest(cards, now, baseline_only=bool(row['baseline']))
                ids = [card.listing_id for card in cards]
                self.db.executemany('INSERT OR IGNORE INTO scan_seen VALUES(?,?)',
                                    [(row['id'], ident) for ident in ids])
                if row['phase'] == 'head_check':
                    stable = ids == json.loads(row['head_ids'])
                    phase = 'complete' if stable else 'blocked'
                    reason = None if stable else 'head_changed_recovery_required'
                    self.db.execute('UPDATE scans SET phase=?,reason=?,last_at=? WHERE id=?',
                                    (phase, reason, now, row['id']))
                    if stable:
                        self.db.execute('UPDATE scan_control SET initial_complete=1,established_scope=?',
                                        (metadata.scope_digest,))
                else:
                    phase = 'head_check' if metadata.terminal_declared else 'pages'
                    next_url = (row['head_url'] or HEAD) if metadata.terminal_declared else metadata.next_url
                    next_index = 0 if metadata.terminal_declared else metadata.page_index + 1
                    head_ids = json.dumps(ids) if row['page_index'] == 0 else row['head_ids']
                    self.db.execute('UPDATE scans SET phase=?,expected=?,page_index=?,scope=?,size=?, '
                                    'head_ids=?,last_at=? WHERE id=?',
                                    (phase, next_url, next_index, metadata.scope_digest, metadata.page_size,
                                     head_ids, now, row['id']))
        return self.status()

    def transition_initial_scope(self, now, *, old_scope, head, evidence_sha256):
        """Explicit initial-baseline transition, NOT equivalence of source scopes.

        Only before any completed baseline. Preserve all observations and the
        blocked parent's reason; fresh pages remain baseline and cannot alert.
        Caller supplies metadata from an observed canonical head and its hash.
        """
        if (type(now) not in (int,float) or not math.isfinite(now) or now <= 0
                or not isinstance(evidence_sha256,str)
                or not re.fullmatch('[a-f0-9]{64}', evidence_sha256)
                or not isinstance(head,PageMetadata) or head.issues
                or head.url != HEAD+'?page=1' or head.page_index != 0
                or not head.scope_digest or not head.page_size
                or (not head.next_url and not head.terminal_declared)):
            raise ValueError('invalid_scope_transition_evidence')
        with self.transaction():
            row = self.status()
            initial, established = self.db.execute(
                'SELECT initial_complete,established_scope FROM scan_control').fetchone()
            if (not row or row['phase'] != 'blocked' or row['baseline'] != 1
                    or row['reason'] != 'scope_metadata_changed' or initial or established
                    or row['scope'] != old_scope or old_scope == head.scope_digest
                    or self.db.execute('SELECT 1 FROM scan_scope_transition').fetchone()):
                raise ValueError('initial_scope_transition_not_applicable')
            if now < row['last_at']:
                raise ValueError('clock_regression')
            if self.db.execute('SELECT count(*) FROM scans').fetchone()[0] >= 100:
                raise ValueError('scan_capacity')
            self.db.execute("UPDATE scans SET phase='abandoned' WHERE id=?", (row['id'],))
            child = self.db.execute(
                'INSERT INTO scans(phase,baseline,expected,page_index,started,last_at,head_url,scope,size) '
                'VALUES(?,?,?,0,?,?,?,?,?)',
                ('pages',1,head.url,now,now,head.url,head.scope_digest,head.page_size)).lastrowid
            self.db.execute('INSERT INTO scan_scope_transition VALUES(?,?,?,?,?,?)',
                (row['id'],child,old_scope,head.scope_digest,evidence_sha256,now))
        return self.status()

    def signal(self, listing_id, now, max_age=300):
        return self.observations.signal(listing_id, now, max_age)
