"""Repeated first-six-page observations, never a complete feed snapshot."""
import math
from .owner_scan import HEAD
from .public_pagination import PageMetadata

class HeadWindow:
    def __init__(self, observations):
        self.obs=observations;self.db=observations.db
        self.db.execute('CREATE TABLE IF NOT EXISTS head_confirmations (id TEXT PRIMARY KEY, first_at REAL NOT NULL, last_at REAL NOT NULL, sightings INTEGER NOT NULL)')

    def accept(self, metadata, cards, now):
        if (type(now) not in (int,float) or not math.isfinite(now) or now<=0
                or not isinstance(metadata,PageMetadata) or metadata.issues
                or metadata.page_index not in range(6)
                or metadata.url!=HEAD+f'?page={metadata.page_index+1}'
                or not metadata.page_size or not cards or len(cards)>metadata.page_size):
            raise ValueError('invalid_head_evidence')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            control=self.db.execute('SELECT initial_complete,established_scope FROM scan_control').fetchone()
            if not control or not control[0] or control[1]!=metadata.scope_digest:
                raise ValueError('head_requires_established_baseline_scope')
            self.obs.ingest(cards,now)
            for card in cards:
                old=self.db.execute('SELECT first_at,last_at,sightings FROM head_confirmations WHERE id=?',(card.listing_id,)).fetchone()
                if old and now<=old[1]:raise ValueError('head_requires_later_observation')
                if old and now-old[0]<=300:
                    self.db.execute('UPDATE head_confirmations SET last_at=?,sightings=sightings+1 WHERE id=?',(now,card.listing_id))
                else:
                    self.db.execute('INSERT OR REPLACE INTO head_confirmations VALUES(?,?,?,1)',(card.listing_id,now,now))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        return {'coverage':'repeated_head_only','whole_market_recall':None,'cards':len(cards)}
