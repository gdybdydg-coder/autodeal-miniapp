"""Offline research only: explicit local inputs, SQLite, injected fake delivery.
No production imports, environment configuration, HTTP or Telegram client.
"""
from dataclasses import asdict
from decimal import Decimal
from html import escape
import json
import hashlib
import sqlite3
from statistics import median, quantiles
from urllib.parse import urlparse
from .prototype import normalize, matches, Car
from .price_review import review


def canonical(raw, now, first_seen=None):
    c = asdict(normalize(raw, now, first_seen=first_seen, source=raw.get('source', 'olx')))
    for key in ('generation', 'body', 'locality', 'engine_cc', 'created_at', 'price_kind', 'vehicle_key', 'checked_at'):
        c[key] = raw.get(key)
    c['photos'] = [p for p in c['photos'] if urlparse(p).scheme == 'https']
    c['evidence'] = raw.get('evidence', 'unverified')
    c['price_review'] = review(raw)
    for key in ('generation','body','locality','vehicle_key'):
        value=c[key]
        c[key]=value.strip() if isinstance(value,str) and value.strip() else None
    for key in ('engine_cc','created_at','checked_at'):
        value=c[key]
        c[key]=value if type(value) is int and value>0 else None
    return c


def filtered(c, f):
    base = Car(**{k: c[k] for k in Car.__dataclass_fields__})
    result = matches(base, f)
    if result is not True:
        return result
    wanted = f.get('body')
    if wanted and c['body'] is not None:
        if c['body'].casefold() not in [x.casefold() for x in (wanted if isinstance(wanted, list) else [wanted])]:
            return False
    return True


def estimate(target, comparables, now, *, minimum=8, max_age=30*86400):
    """Experimental conservative asking-price comparison, NOT a sale appraisal.
    Same-currency only; absent FX intentionally yields unknown, never an invented rate.
    """
    unknown = lambda reason, n=0: {'status': 'profitability_unconfirmed', 'reason': reason, 'sample': n}
    keys = ('brand', 'model', 'generation', 'body', 'fuel', 'transmission', 'engine_cc')
    if target.get('price_review',{}).get('status') == 'needs_review':
        reasons=target['price_review'].get('reasons',[])
        return unknown('full_price_unconfirmed' if reasons==['full_price_unconfirmed'] else 'price_evidence_conflict')
    if target['price_kind'] != 'full' or target['price'] is None or target['currency'] is None:
        return unknown('full_price_unconfirmed')
    if target['category'] != 'whole_passenger_car':
        return unknown('not_a_whole_car')
    if any(target.get(k) is None for k in (*keys, 'year', 'mileage_km')):
        return unknown('missing_comparison_attributes')
    seen, values = set(), []
    for c in comparables:
        identity = c.get('vehicle_key') or (c['source'], c['id'])
        if identity in seen or (c['source'], c['id']) == (target['source'], target['id']):
            continue
        if target.get('vehicle_key') and identity == target['vehicle_key']:
            continue
        checked = c.get('checked_at')
        if type(checked) is not int:
            continue
        age = now - checked
        if not 0 <= age <= max_age or c['currency'] != target['currency'] or c['price_kind'] != 'full':
            continue
        if c.get('price_review',{}).get('status') == 'needs_review':
            continue
        if c['category'] != 'whole_passenger_car' or c['price'] is None:
            continue
        if any(c.get(k) != target.get(k) for k in keys):
            continue
        if c['year'] is None or c['mileage_km'] is None or abs(c['year']-target['year']) > 1 or abs(c['mileage_km']-target['mileage_km']) > 30000:
            continue
        seen.add(identity)
        values.append(float(c['price']))
    if len(values) < minimum:
        return unknown('insufficient_comparables', len(values))
    q1, _, q3 = quantiles(values, n=4, method='inclusive')
    iqr = q3-q1
    values = [v for v in values if q1-1.5*iqr <= v <= q3+1.5*iqr]
    if len(values) < minimum:
        return unknown('insufficient_after_outliers', len(values))
    center = median(values)
    if (max(values)-min(values))/center > .4:
        return unknown('wide_price_dispersion', len(values))
    floor = quantiles(values, n=4, method='inclusive')[0] * .95
    return {'status': 'experimental_estimate', 'sample': len(values), 'asking_median': center,
            'conservative_reference': floor, 'discount_percent': 100*(1-float(target['price'])/floor),
            'currency': target['currency'], 'method': 'matched_asking_q25_margin_5pct',
            'real_world_accuracy_verified': False}


def fingerprint(c):
    # Observation time may advance without changing the car or its price.
    semantic = {k:v for k,v in c.items() if k not in ('checked_at','first_seen_at','updated_at','bumped_at')}
    return hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def source_selection(value=None):
    choices = {'AUTO.RIA': ('auto_ria',), 'OLX': ('olx',), 'Обидва джерела': ('auto_ria','olx')}
    return choices[value] if value is not None else ('auto_ria',)


def message(c):
    fields = [c.get('title') or 'Автомобіль', f"{c['price']} {c['currency']}" if c['price'] and c['currency'] else 'Ціну не підтверджено']
    fields += [str(c[k]) for k in ('year','locality','mileage_km','fuel','transmission') if c.get(k) is not None]
    fields += ['Джерело: OLX', 'Тестовий приклад. Оцінка експериментальна.']
    url = c.get('url')
    valid = url and urlparse(url).scheme == 'https' and urlparse(url).hostname in ('www.olx.ua','olx.ua','example.invalid')
    return {'text': '\n'.join(escape(x) for x in fields), 'photo': next(iter(c['photos']), None),
            'button': {'text': 'Відкрити оголошення', 'url': url} if valid else None}


class Pipeline:
    def __init__(self, path):
        if '://' in str(path) or str(path) == ':memory:':
            raise ValueError('Explicit durable local research database required')
        self.db = sqlite3.connect(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY, boundary INTEGER, cursor TEXT, complete INTEGER, last_success INTEGER, retry_at INTEGER, attempts INTEGER);
        INSERT OR IGNORE INTO checkpoint VALUES(1,NULL,NULL,0,NULL,0,0);
        CREATE TABLE IF NOT EXISTS listings(source TEXT,id TEXT,payload TEXT,eligibility TEXT,PRIMARY KEY(source,id));
        CREATE TABLE IF NOT EXISTS deliveries(uid TEXT,source TEXT,id TEXT,status TEXT,PRIMARY KEY(uid,source,id));
        CREATE TABLE IF NOT EXISTS runs(at INTEGER,status TEXT,pages INTEGER,rows INTEGER,reason TEXT);
        CREATE TABLE IF NOT EXISTS delivery_proof(uid TEXT,source TEXT,id TEXT,fingerprint TEXT,expires INTEGER,PRIMARY KEY(uid,source,id));
        CREATE TABLE IF NOT EXISTS page_cursors(cursor TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS assessments(source TEXT,id TEXT,at INTEGER,fingerprint TEXT,result TEXT,PRIMARY KEY(source,id));
        CREATE INDEX IF NOT EXISTS delivery_status ON deliveries(status);
        CREATE INDEX IF NOT EXISTS listing_eligibility ON listings(eligibility);
        ''')
        # A crash after invoking a sender is ambiguous. Never silently replay it.
        self.db.execute("UPDATE deliveries SET status='uncertain' WHERE status='sending'")
        self.db.commit()

    def collect(self, fetch, now, *, page_budget, row_budget):
        if page_budget < 1 or row_budget < 1:
            raise ValueError('Positive explicit budgets required')
        boundary, cursor, complete, last, retry, attempts = self.db.execute('SELECT boundary,cursor,complete,last_success,retry_at,attempts FROM checkpoint').fetchone()
        if attempts >= 3:
            return {'status': 'paused_manual_review', 'pages': 0, 'rows': 0}
        if now < retry:
            return {'status': 'paused', 'pages': 0, 'rows': 0}
        if boundary is None:
            boundary = now
            self.db.execute('UPDATE checkpoint SET boundary=?', (boundary,)); self.db.commit()
        if complete:
            cursor = None
            self.db.execute('DELETE FROM page_cursors'); self.db.commit()
        pages, rows, visited, status, reason = 0, 0, set(), 'incomplete', 'page_budget'
        while pages < page_budget:
            if cursor in visited or self.db.execute('SELECT 1 FROM page_cursors WHERE cursor=?',(json.dumps(cursor),)).fetchone():
                reason = 'cursor_cycle'; break
            visited.add(cursor)
            try:
                page = fetch(cursor)
                cards, next_cursor = page['items'], page['next']
                if type(cards) is not list or (next_cursor is not None and not isinstance(next_cursor,str)):
                    raise ValueError('invalid page contract')
                if rows + len(cards) > row_budget:
                    reason = 'row_budget'; break
                records = []
                for raw in cards:
                    if not isinstance(raw,dict):
                        raise ValueError('invalid listing contract')
                    source = raw.get('source','olx')
                    old = self.db.execute('SELECT payload,eligibility FROM listings WHERE source=? AND id=?', (source,raw.get('id'))).fetchone()
                    c = canonical(raw, now, json.loads(old[0])['first_seen_at'] if old else None)
                    eligible = ('new' if boundary <= c['published_at'] <= now else 'baseline') if c['publication_verified'] else 'unverified'
                    if old and old[1] in ('new', 'baseline'):
                        eligible = old[1]
                    records.append((source,c['id'],json.dumps(c),eligible))
                with self.db:
                    self.db.execute('INSERT INTO page_cursors VALUES(?)',(json.dumps(cursor),))
                    self.db.executemany('INSERT INTO listings VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload,eligibility=excluded.eligibility', records)
                    self.db.execute('UPDATE checkpoint SET cursor=?,complete=?,last_success=?,retry_at=0,attempts=0', (next_cursor,int(next_cursor is None),now if next_cursor is None else last))
                attempts = 0
                rows += len(cards); pages += 1; cursor = next_cursor
                if cursor is None:
                    status, reason = 'complete', None; break
            except (TimeoutError, ConnectionError, ValueError, KeyError) as exc:
                attempts += 1
                reason = type(exc).__name__
                with self.db:
                    self.db.execute('UPDATE checkpoint SET complete=0,retry_at=?,attempts=?', (now+min(3600,30*2**min(attempts,7)),attempts))
                break
        with self.db:
            if status != 'complete':
                self.db.execute('UPDATE checkpoint SET complete=0')
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(now,status,pages,rows,reason))
        return {'status':status,'pages':pages,'rows':rows,'reason':reason}

    def cars(self, only_new=False):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM listings' + (" WHERE eligibility='new'" if only_new else ''))]

    def assessment_summary(self):
        """Saved decisions, explicitly distinguish changed cards from current evidence."""
        totals = dict(listings=0, unassessed=0, changed_since_assessment=0, experimental=0, unknown=0, reasons={})
        for payload, saved, result in self.db.execute('SELECT l.payload,a.fingerprint,a.result FROM listings l LEFT JOIN assessments a ON l.source=a.source AND l.id=a.id'):
            totals['listings'] += 1
            if saved is None:
                totals['unassessed'] += 1; continue
            if fingerprint(json.loads(payload)) != saved:
                totals['changed_since_assessment'] += 1; continue
            decision=json.loads(result)
            if decision['status']=='experimental_estimate':
                totals['experimental'] += 1
            else:
                totals['unknown'] += 1
                reason=decision['reason'];totals['reasons'][reason]=totals['reasons'].get(reason,0)+1
        return totals

    def enqueue(self, users, comparisons, now, *, capacity, olx_enabled=False):
        counts = dict(queued=0, filtered=0, uncertain=0, denied=0, overflow=0)
        pending = self.db.execute("SELECT count(*) FROM deliveries WHERE status='pending'").fetchone()[0]
        for c in self.cars(True):
            assessment = estimate(c, comparisons, now)
            self.db.execute('INSERT OR REPLACE INTO assessments VALUES(?,?,?,?,?)',(c['source'],c['id'],now,fingerprint(c),json.dumps(assessment)))
            for u in users:
                if c['source'] not in source_selection(u.get('source')) or (c['source']=='olx' and not olx_enabled):
                    continue
                if not u['paid'] or u['stopped']:
                    counts['denied'] += 1; continue
                match = filtered(c,u['filters'])
                if match is False:
                    counts['filtered'] += 1; continue
                if match is None or assessment['status'] != 'experimental_estimate':
                    counts['uncertain'] += 1; continue
                if assessment['discount_percent'] < u.get('min_discount',15):
                    counts['filtered'] += 1; continue
                key = (str(u['id']),c['source'],c['id'])
                existing = self.db.execute('SELECT status FROM deliveries WHERE uid=? AND source=? AND id=?',key).fetchone()
                if existing and existing[0] != 'needs_revalidation':
                    continue
                if pending >= capacity:
                    counts['overflow'] += 1; continue
                self.db.execute("INSERT INTO deliveries VALUES(?,?,?,'pending') ON CONFLICT(uid,source,id) DO UPDATE SET status='pending'",key)
                self.db.execute('INSERT OR REPLACE INTO delivery_proof VALUES(?,?,?,?,?)',(*key,fingerprint(c),now+300))
                counts['queued'] += 1; pending += 1
        self.db.commit()
        return counts

    def deliver_fake(self, sender, access, *, now, olx_enabled=False):
        """Caller must supply a fake sender. This module ships no live sender adapter."""
        counts = dict(accepted=0, denied=0, uncertain=0, held=0)
        for uid,source,id in self.db.execute("SELECT uid,source,id FROM deliveries WHERE status='pending'").fetchall():
            if source=='olx' and not olx_enabled:
                continue
            if not access(uid):
                counts['denied'] += 1; continue
            key=(uid,source,id)
            c=json.loads(self.db.execute('SELECT payload FROM listings WHERE source=? AND id=?',(source,id)).fetchone()[0])
            proof=self.db.execute('SELECT fingerprint,expires FROM delivery_proof WHERE uid=? AND source=? AND id=?',key).fetchone()
            if proof is None or proof[0] != fingerprint(c) or now > proof[1]:
                self.db.execute("UPDATE deliveries SET status='needs_revalidation' WHERE uid=? AND source=? AND id=?",key); self.db.commit()
                counts['held'] += 1; continue
            self.db.execute("UPDATE deliveries SET status='sending' WHERE uid=? AND source=? AND id=?",key); self.db.commit()
            try:
                result=sender(uid,message(c))
                state='accepted' if result is True else 'rejected'
            except TimeoutError:
                state='uncertain'
            self.db.execute('UPDATE deliveries SET status=? WHERE uid=? AND source=? AND id=?',(state,*key)); self.db.commit()
            if state in counts: counts[state]+=1
        return counts
