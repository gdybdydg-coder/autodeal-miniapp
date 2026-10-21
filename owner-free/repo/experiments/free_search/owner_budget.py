"""Durable bounded public-fetch ledger; no HTTP, production DB or sender.

SQLite BEGIN IMMEDIATE serializes independent workers. Reservations are spent
before transport and never refunded, including after crash. This is an isolated
component, not activation of the owner's running bot.
"""
import math
import re
import sqlite3
from urllib.parse import urlsplit

UA = 'AutoDeal-Research/0.1'


class Denied(ValueError):
    pass


def public_url(url, *, allow_feed_pagination=False, allow_source_assets=False):
    try:
        u = urlsplit(url)
    except ValueError:
        raise Denied('invalid_url')
    page_two = ((u.path == '/uk/last/hour/' and u.query in ('page=1','page=2')) or
        (u.path == '/uk/car/volkswagen/tiguan/year/2022/' and u.query in ('page=2','page=3')))
    if allow_feed_pagination and u.path == '/uk/last/hour/':
        from .public_pagination import route_page
        page_two = route_page(url) is not None
    if (u.scheme != 'https' or u.netloc != 'auto.ria.com' or (u.query and not page_two)
            or u.fragment or '%' in u.path):
        raise Denied('non_public_url')
    if not ((allow_source_assets and re.fullmatch(r'/vue3-prod/static/[A-Za-z0-9_-]+\.js',u.path)) or
            u.path in ('/robots.txt', '/uk/last/hour/',
                      '/uk/car/audi/a6/c7-4g-fl/year/2015/') or re.fullmatch(
            r'/uk/auto_[A-Za-z0-9_-]+_[1-9][0-9]{0,11}\.html', u.path)
            or re.fullmatch(r'/uk/car/[a-z0-9-]+/[a-z0-9-]+/(?:year/\d{4}/)?', u.path)):
        raise Denied('non_public_url')


class Budget:
    def __init__(self, path, max_gets, max_bytes, *, allow_feed_pagination=False, allow_source_assets=False):
        if type(allow_feed_pagination) is not bool or type(allow_source_assets) is not bool:
            raise Denied('invalid_pagination_policy')
        self.allow_feed_pagination = allow_feed_pagination
        self.allow_source_assets = allow_source_assets
        if type(max_gets) is not int or type(max_bytes) is not int or min(max_gets, max_bytes) <= 0:
            raise Denied('invalid_limits')
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS budget (
                id INTEGER PRIMARY KEY CHECK(id=1), max_gets INTEGER,
                max_bytes INTEGER, hold INTEGER DEFAULT 0, robots_until REAL DEFAULT 0,
                next_at REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS observations (url TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, url TEXT, cap INTEGER, started REAL,
                completed REAL, status INTEGER, bytes INTEGER);
        ''')
        self.db.execute('INSERT OR IGNORE INTO budget(id,max_gets,max_bytes) VALUES(1,?,?)',
                        (max_gets, max_bytes))
        if self.db.execute('SELECT max_gets,max_bytes FROM budget').fetchone() != (max_gets,max_bytes):
            self.db.close()
            raise Denied('persistent_limits_differ')

    def close(self):
        self.db.close()

    def observe(self, url):
        # Caller must obtain URLs from public HTML, never invent target IDs.
        public_url(url, allow_feed_pagination=self.allow_feed_pagination,allow_source_assets=self.allow_source_assets)
        self.db.execute('INSERT OR IGNORE INTO observations VALUES(?)', (url,))

    def reserve(self, url, cap, now, robots_allowed=False):
        public_url(url, allow_feed_pagination=self.allow_feed_pagination,allow_source_assets=self.allow_source_assets)
        if (type(cap) is not int or not 0 < cap <= 2500000
                or type(now) not in (int, float) or not math.isfinite(now) or now <= 0):
            raise Denied('invalid_reservation')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            mg, mb, hold, robots_until, next_at = self.db.execute(
                'SELECT max_gets,max_bytes,hold,robots_until,next_at FROM budget').fetchone()
            if hold:
                raise Denied('persistent_source_hold')
            if self.db.execute('SELECT 1 FROM requests WHERE completed IS NULL').fetchone():
                raise Denied('inflight_or_crashed_request')
            if now < next_at:
                raise Denied('completion_pause')
            if not self.db.execute('SELECT 1 FROM observations WHERE url=?', (url,)).fetchone():
                raise Denied('unobserved_url')
            if url != 'https://auto.ria.com/robots.txt' and (robots_until <= now or robots_allowed is not True):
                raise Denied('robots_required')
            count, spent = self.db.execute('SELECT count(*),coalesce(sum(cap),0) FROM requests').fetchone()
            if count >= mg or spent + cap > mb:
                raise Denied('budget_exhausted')
            ident = self.db.execute('INSERT INTO requests(url,cap,started) VALUES(?,?,?)',
                                    (url,cap,now)).lastrowid
            self.db.execute('COMMIT')
            return ident
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def complete(self, ident, now, status, body_bytes, challenge=False, robots_valid=False, pause=4):
        if (type(now) not in (int,float) or not math.isfinite(now)
                or type(body_bytes) is not int or body_bytes < 0
                or type(pause) not in (int,float) or not math.isfinite(pause) or pause < 4
                or (status is not None and (type(status) is not int or not 100 <= status <= 599))):
            raise Denied('invalid_completion')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT url,cap,started,completed FROM requests WHERE id=?', (ident,)).fetchone()
            if row is None or row[3] is not None or now < row[2]:
                raise Denied('unknown_or_completed_request')
            self.db.execute('UPDATE requests SET completed=?,status=?,bytes=? WHERE id=?',
                            (now,status,body_bytes,ident))
            hold = bool(challenge or status in (401,403,429) or body_bytes > row[1])
            self.db.execute('UPDATE budget SET hold=max(hold,?),next_at=?', (int(hold),now+pause))
            if row[0] == 'https://auto.ria.com/robots.txt':
                valid = robots_valid is True and status == 200 and body_bytes > 0 and not hold
                self.db.execute('UPDATE budget SET robots_until=?', (now+3600 if valid else 0,))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def summary(self):
        count, reserved, received = self.db.execute(
            'SELECT count(*),coalesce(sum(cap),0),coalesce(sum(bytes),0) FROM requests').fetchone()
        return dict(reserved_gets=count, reserved_bytes=reserved, received_bytes=received,
                    source_hold=bool(self.db.execute('SELECT hold FROM budget').fetchone()[0]))
