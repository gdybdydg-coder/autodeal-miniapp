"""Persistent public pagination, head revisits, overlap and transport backoff.

Only validated next links are followed. A cycle is not an atomic market snapshot.
Legacy control/history remain intact; the old page-count field is not a limit.
"""
import time
from .owner_continuous_intake import IntakeState
from .owner_scan import HEAD
from .public_pagination import route_page


class SourceRetry(ValueError):
    pass


def transient_receipt(receipt):
    status, error = receipt.get('status'), receipt.get('error')
    if error in ('source_challenge', 'body_cap_reached') or status in (401, 403, 429):
        return False
    return (status in (408, 500, 502, 503, 504) or
            status in (None, 200) and error in (
                'UnicodeDecodeError', 'incomplete_response', 'http_protocol_error',
                'URLError', 'TimeoutError', 'ConnectionResetError',
                'ConnectionAbortedError', 'RemoteDisconnected'))


class DynamicIntakeState(IntakeState):
    def __init__(self, queue, *, pages=6, head_seconds=12, clock=time.time):
        super().__init__(queue, pages=pages, head_seconds=head_seconds)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS intake_dynamic (
            id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL, frontier TEXT,
            overlap TEXT, role TEXT, forwards INTEGER, cycles INTEGER, max_page INTEGER);
          CREATE TABLE IF NOT EXISTS intake_links (page INTEGER PRIMARY KEY, url TEXT);
          CREATE TABLE IF NOT EXISTS intake_recovery (
            id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER, retry_at REAL,
            url TEXT, error TEXT, status INTEGER, failed_at REAL);
        ''')
        self.db.execute('INSERT OR IGNORE INTO intake_recovery VALUES(1,0,0,NULL,NULL,NULL,NULL)')
        self.db.execute('INSERT OR IGNORE INTO intake_links VALUES(1,?)', (HEAD+'?page=1',))
        if not self.db.execute('SELECT 1 FROM intake_dynamic').fetchone():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute("INSERT INTO intake_dynamic VALUES(1,?,NULL,NULL,'head',0,0,0)", (clock(),))
                # Change the cursor policy once, never reset observations or sends.
                self.db.execute('UPDATE intake_control SET next_page=1,next_head=0')
                self.record('dynamic_migration', {'legacy_pages': pages}, clock())
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK'); raise

    def url_for(self, page):
        row = self.db.execute('SELECT url FROM intake_links WHERE page=?', (page,)).fetchone()
        if row is None or route_page(row[0]) != page-1:
            raise ValueError('unobserved_pagination_link')
        return row[0]

    def recovery(self):
        row = self.db.execute('SELECT failures,retry_at,url,error,status,failed_at FROM intake_recovery').fetchone()
        return dict(zip(('failures','retry_at','url','error','status','failed_at'), row))

    def defer(self, receipt, now):
        if not transient_receipt(receipt):
            raise ValueError('nontransient_source_failure')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            failures = self.recovery()['failures']+1
            delay = min(900, 30 * 2**min(failures-1, 5))
            self.db.execute('UPDATE intake_recovery SET failures=?,retry_at=?,url=?,error=?,status=?,failed_at=?',
                (failures, now+delay, receipt['url'], receipt['error'], receipt['status'], now))
            self.record('source_backoff', {'receipt':receipt, 'failures':failures, 'retry_at':now+delay}, now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def success(self, receipt, now):
        old = self.recovery()
        if old['failures'] and (old['url'] == receipt['url'] or not receipt['url'].endswith('/robots.txt')):
            self.db.execute('UPDATE intake_recovery SET failures=0,retry_at=0,url=NULL,error=NULL,status=NULL,failed_at=NULL')
            self.record('source_recovered', {'failures':old['failures'], 'url':receipt['url']}, now)

    def action(self, now, detail_available):
        if self.db.execute('SELECT stopped FROM intake_control').fetchone()[0]:
            return 'stop', None
        retry = self.recovery()['retry_at']
        if now < retry:
            return 'wait', retry
        return super().action(now, detail_available)

    def accept_page(self, metadata, cards, now, evidence):
        page = metadata.page_index+1
        if metadata.issues or metadata.url != self.url_for(page):
            raise ValueError('invalid_dynamic_page')
        if self.db.execute('SELECT next_page FROM intake_control').fetchone()[0] != page:
            raise ValueError('head_cursor_conflict')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            frontier, overlap, role, forwards = self.db.execute(
                'SELECT frontier,overlap,role,forwards FROM intake_dynamic').fetchone()
            next_url = metadata.next_url
            if next_url:
                if route_page(next_url) != page:
                    raise ValueError('invalid_next_page')
                self.db.execute('INSERT OR REPLACE INTO intake_links VALUES(?,?)', (page+1,next_url))
            if page == 1:
                frontier = frontier or next_url
                chosen = overlap or frontier
                if chosen:
                    missing=self.db.execute('SELECT retry_after FROM intake_missing_pages WHERE page=?',
                                            (route_page(chosen)+1,)).fetchone()
                    if missing and now < missing[0]:
                        frontier=next_url
                        chosen=next_url if next_url!=chosen else None
                        overlap=None
                next_page = route_page(chosen)+1 if chosen else 1
                role = 'overlap' if overlap else 'forward' if chosen else 'head'
                overlap = None
            else:
                next_page = 1
                if role == 'forward':
                    frontier = next_url
                    forwards += 1
                    if metadata.terminal_declared:
                        self.db.execute('UPDATE intake_dynamic SET cycles=cycles+1')
                        self.record('declared_end', {'page':page, 'whole_market_recall':None}, now)
                        overlap = None
                    elif forwards % 2 == 0 and page > 2:
                        overlap = self.url_for(page-1)
                role = 'head'
            for card in cards:
                self.db.execute('INSERT INTO intake_previews(id,price,observed_at,city) VALUES(?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at,city=excluded.city',
                    (card.listing_id,str(card.preview_usd) if card.preview_usd is not None else None,
                     now,getattr(card,'preview_city',None)))
            self.db.execute('UPDATE intake_dynamic SET frontier=?,overlap=?,role=?,forwards=?,max_page=max(max_page,?)',
                (frontier,overlap,role,forwards,page))
            self.db.execute('UPDATE intake_control SET next_page=?,next_head=?', (next_page,now+self.head_seconds))
            self.record('head',dict(evidence, traversal='dynamic_observed_links'),now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def missing_head(self, page, now, receipt):
        if page <= 1 or receipt.get('status') != 404:
            raise ValueError('invalid_missing_head_evidence')
        # Do not invent page+1 or require the disappeared page to return.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR REPLACE INTO intake_missing_pages VALUES(?,?)',(page,now+600))
            self.db.execute("UPDATE intake_dynamic SET frontier=NULL,overlap=NULL,role='head'")
            self.db.execute('UPDATE intake_control SET next_page=1,next_head=?', (now+self.head_seconds,))
            self.record('missing_head', {'page':page,'receipt':receipt,'whole_market_recall':None},now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise
