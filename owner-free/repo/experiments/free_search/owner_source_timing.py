"""Local comparison evidence, no delivery authority or paid API/cache imports.

Only compare equal listing/search activation identities. First observations are
immutable; missing source or receipt stays unknown. Never infer publication.
"""
import math
import sqlite3


class SourceTiming:
    def __init__(self,path):
        self.db=sqlite3.connect(path,isolation_level=None,timeout=5)
        self.db.execute('CREATE TABLE IF NOT EXISTS source_timing ('
          'owner INTEGER,listing TEXT,search INTEGER,epoch TEXT,source TEXT,'
          'first_seen REAL,accepted REAL,message INTEGER,'
          'PRIMARY KEY(owner,listing,search,epoch,source))')

    def close(self):self.db.close()

    def observe(self,owner,listing,search,epoch,source,first_seen):
        if (type(owner) is not int or owner<=0 or type(search) is not int or search<=0
                or not isinstance(listing,str) or not listing.isascii() or not listing.isdigit()
                or not isinstance(epoch,str) or not epoch or source not in ('paid','free')
                or type(first_seen) not in (int,float) or not math.isfinite(first_seen) or first_seen<=0):
            raise ValueError('invalid_source_observation')
        self.db.execute('INSERT OR IGNORE INTO source_timing VALUES(?,?,?,?,?,?,NULL,NULL)',
                        (owner,listing,search,epoch,source,first_seen))

    def receipt(self,owner,listing,search,epoch,source,accepted,message):
        if (type(accepted) not in (int,float) or not math.isfinite(accepted)
                or type(message) is not int or message<=0):raise ValueError('invalid_source_receipt')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT first_seen,accepted,message FROM source_timing '
              'WHERE owner=? AND listing=? AND search=? AND epoch=? AND source=?',
              (owner,listing,search,epoch,source)).fetchone()
            if not row or accepted<row[0]:raise ValueError('missing_or_reversed_observation')
            if row[1] is not None and (row[1],row[2])!=(accepted,message):
                raise ValueError('conflicting_source_receipt')
            self.db.execute('UPDATE source_timing SET accepted=?,message=? WHERE '
               'owner=? AND listing=? AND search=? AND epoch=? AND source=?',
               (accepted,message,owner,listing,search,epoch,source))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def compare(self,owner,listing,search,epoch):
        rows={r[0]:{'first_seen':r[1],'accepted_at':r[2],'message_id':r[3]} for r in
              self.db.execute('SELECT source,first_seen,accepted,message FROM source_timing '
                'WHERE owner=? AND listing=? AND search=? AND epoch=?',(owner,listing,search,epoch))}
        def delta(field):
            a,b=rows.get('free',{}).get(field),rows.get('paid',{}).get(field)
            return None if a is None or b is None else a-b
        return {'listing_id':listing,'search_id':search,'epoch':epoch,'sources':rows,
                'free_minus_paid_discovery_seconds':delta('first_seen'),
                'free_minus_paid_acceptance_seconds':delta('accepted_at'),
                'publication_at':None,'negative_delta_means':'free_earlier',
                'acceptance_is_phone_notification_time':False}
