"""Synthetic input contract ONLY. No OLX endpoints, production imports or network.

Official partner API access to other users' ads is not established. Adapters must
not be wired to public scraping/credentials just because this fixture works.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import sqlite3

WINDOW = 3600


@dataclass(frozen=True)
class Car:
    source: str
    id: str
    url: str | None
    title: str | None
    price: str | None
    currency: str | None
    brand: str | None
    model: str | None
    year: int | None
    region: str | None
    mileage_km: int | None
    fuel: str | None
    transmission: str | None
    photos: tuple
    published_at: int | None
    updated_at: int | None
    bumped_at: int | None
    first_seen_at: int
    publication_verified: bool
    category: str | None
    condition: str | None
    issues: tuple
    valuation_status: str = 'profitability_unconfirmed'


def timestamp(value):
    return value if type(value) is int and value > 0 else None


def normalize(raw, now, *, source='olx', first_seen=None):
    if type(raw) is not dict or not isinstance(raw.get('id'),str) or not raw['id'] or len(raw['id'])>100:
        raise ValueError('Stable source ID required')
    if source not in ('olx','auto_ria') or timestamp(now) is None:
        raise ValueError('Invalid fixture source/time')
    issues=[]
    # The fixture's explicit taxonomy is not presented as official OLX IDs.
    category=raw.get('category')
    if category != 'whole_passenger_car':issues.append('category_not_confirmed_car')
    try:
        if isinstance(raw.get('price'),bool):raise InvalidOperation
        price=Decimal(str(raw.get('price')))
        if not price.is_finite() or price <= 1:
            issues.append('placeholder_or_missing_price');price=None
    except InvalidOperation:
        price=None;issues.append('placeholder_or_missing_price')
    currency=raw.get('currency')
    if currency not in ('UAH','USD','EUR'):issues.append('unknown_currency')
    if raw.get('price_kind') != 'full':issues.append('full_price_unconfirmed')
    published=timestamp(raw.get('published_at'))
    verified=raw.get('publication_verified') is True and published is not None
    if not verified:issues.append('publication_unverified')
    if published and published>now:issues.append('future_publication')
    def string(key):
        value=raw.get(key)
        return value.strip() if isinstance(value,str) and value.strip() else None
    def integer(key,minimum,maximum):
        value=raw.get(key)
        return value if type(value) is int and minimum<=value<=maximum else None
    photos=raw.get('photos')
    photos=tuple(p for p in photos if isinstance(p,str)) if isinstance(photos,list) else ()
    return Car(source,raw['id'],string('url'),string('title'),str(price) if price is not None else None,
               currency if currency in ('UAH','USD','EUR') else None,string('brand'),string('model'),
               integer('year',1886,2100),string('region'),integer('mileage_km',0,10**7),
               string('fuel'),string('transmission'),photos,published,timestamp(raw.get('updated_at')),
               timestamp(raw.get('bumped_at')),first_seen or now,verified,category,string('condition'),tuple(issues))


def matches(car, filters):
    if 'category_not_confirmed_car' in car.issues:return False
    # Missing optional information is not a contradiction; known conflicts reject.
    for key in ('brand','model','region','fuel','transmission'):
        wanted=filters.get(key);actual=getattr(car,key)
        if wanted and actual is not None:
            values=wanted if isinstance(wanted,list) else [wanted]
            if actual.casefold() not in [v.casefold() for v in values]:return False
    for key in ('year','mileage_km'):
        actual=getattr(car,key)
        if actual is not None:
            if filters.get(key+'_min') is not None and actual<filters[key+'_min']:return False
            if filters.get(key+'_max') is not None and actual>filters[key+'_max']:return False
    # No silent exchange-rate assumption. Currency mismatch is unresolved.
    if filters.get('price_min') is not None or filters.get('price_max') is not None:
        if car.price is None or car.currency != filters.get('currency'):return None
        price=Decimal(car.price)
        if filters.get('price_min') is not None and price<Decimal(str(filters['price_min'])):return False
        if filters.get('price_max') is not None and price>Decimal(str(filters['price_max'])):return False
    if any(x in car.issues for x in ('placeholder_or_missing_price','unknown_currency','full_price_unconfirmed')):return None
    return True


def possible_duplicate(a,b):
    """A review hint, never a delete/merge/delivery suppression decision."""
    if a.source==b.source and a.id==b.id:return 'same_source_id'
    if a.source!=b.source and all(getattr(a,k) is not None and getattr(a,k)==getattr(b,k)
                                 for k in ('brand','model','year','price','currency','region')):
        return 'possible_cross_source_duplicate'
    return None


class Store:
    def __init__(self,path):
        # Explicit local file only. No environment, DSN or automatic connection.
        if '://' in str(path):raise ValueError('Local SQLite fixture path only')
        self.db=sqlite3.connect(path)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),baseline INTEGER,cursor TEXT,last_success INTEGER);
          CREATE TABLE IF NOT EXISTS cars(source TEXT,id TEXT,first_seen INTEGER,payload TEXT,PRIMARY KEY(source,id));
          CREATE TABLE IF NOT EXISTS publications(source TEXT,id TEXT,published INTEGER,status TEXT,PRIMARY KEY(source,id,published));
          CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,at INTEGER,status TEXT,calls INTEGER,error TEXT);
          CREATE TABLE IF NOT EXISTS fake_delivery(uid INTEGER,source TEXT,id TEXT,status TEXT,PRIMARY KEY(uid,source,id));
          INSERT OR IGNORE INTO state VALUES(1,NULL,NULL,NULL);
        ''')

    def collect(self, transport, now, *, max_pages=20, max_cards=1000):
        if timestamp(now) is None or type(max_pages) is not int or not 1<=max_pages<=20:
            raise ValueError('Invalid bounded run')
        baseline,cursor,last=self.db.execute('SELECT baseline,cursor,last_success FROM state').fetchone()
        since=max(baseline or now,(last or now)-WINDOW)
        calls=0;pages=set();rows=[];ids={};next_cursor=cursor
        try:
            while True:
                if calls>=max_pages:raise ValueError('page_budget_exhausted')
                key=next_cursor
                if key in pages:raise ValueError('pagination_cycle')
                pages.add(key);calls+=1
                page=transport.fetch(cursor=key,since=since)
                if type(page) is not dict or type(page.get('items')) is not list:raise ValueError('invalid_page')
                for raw in page['items']:
                    if len(rows)>=max_cards:raise ValueError('card_budget_exhausted')
                    car=normalize(raw,now)
                    sig=json.dumps(raw,sort_keys=True)
                    if car.id in ids and ids[car.id]!=sig:raise ValueError('moving_page_conflict')
                    if car.id not in ids:rows.append(car);ids[car.id]=sig
                next_cursor=page.get('next')
                if next_cursor is None:break
                if not isinstance(next_cursor,str) or not 1<=len(next_cursor)<=256:raise ValueError('invalid_cursor')
            checkpoint=page.get('checkpoint')
            if checkpoint is not None and (not isinstance(checkpoint,str) or not 1<=len(checkpoint)<=256):raise ValueError('invalid_checkpoint')
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                current=self.db.execute('SELECT baseline,cursor,last_success FROM state').fetchone()
                if current!=(baseline,cursor,last):raise ValueError('concurrent_cursor_change')
                for car in rows:
                    existing=self.db.execute('SELECT first_seen FROM cars WHERE source=? AND id=?',(car.source,car.id)).fetchone()
                    payload=asdict(car)
                    if existing:payload['first_seen_at']=existing[0]
                    self.db.execute('INSERT INTO cars VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload',
                        (car.source,car.id,payload['first_seen_at'],json.dumps(payload,ensure_ascii=False)))
                    fresh=(baseline is not None and car.publication_verified and not car.issues and
                           max(baseline,now-WINDOW)<car.published_at<=now)
                    status='fresh_unvalued' if fresh else ('baseline' if baseline is None else 'retained_not_sendable')
                    self.db.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,?)',
                                    (car.source,car.id,car.published_at or 0,status))
                self.db.execute('UPDATE state SET baseline=?,cursor=?,last_success=? WHERE id=1',(baseline or now,checkpoint,now))
                self.db.execute('INSERT INTO runs(at,status,calls) VALUES(?,?,?)',(now,'committed',calls))
            return {'status':'committed','calls':calls,'cards':len(rows),'since':since}
        except (ValueError,TimeoutError) as exc:
            self.db.rollback()
            reason=str(exc) if isinstance(exc,ValueError) else 'timeout'
            with self.db:self.db.execute('INSERT INTO runs(at,status,calls,error) VALUES(?,?,?,?)',(now,'retryable',calls,reason))
            return {'status':'retryable','calls':calls,'reason':reason}

    def cached(self):
        return [Car(**json.loads(row[0])) for row in self.db.execute('SELECT payload FROM cars ORDER BY source,id')]

    def fanout(self, users):
        """Shared cached collection. NO Telegram; unvalued cards never queue as deals."""
        count={'users':len(users),'cars':0,'comparisons':0,'matching_unvalued_pairs':0,'unresolved_pairs':0,'fake_sends':0}
        for car in self.cached():
            count['cars']+=1
            for uid,filters,enabled in users:
                if not enabled:continue
                count['comparisons']+=1
                result=matches(car,filters)
                if result is True:count['matching_unvalued_pairs']+=1
                elif result is None:count['unresolved_pairs']+=1
        return count

    def close(self):self.db.close()


class FixtureTransport:
    def __init__(self,pages):self.pages=pages;self.calls=[]
    def fetch(self,*,cursor,since):
        self.calls.append((cursor,since))
        page=self.pages[cursor]
        if isinstance(page,Exception):raise page
        return page
