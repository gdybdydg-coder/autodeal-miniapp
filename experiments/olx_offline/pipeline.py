"""Offline research only: explicit local inputs, SQLite, injected fake delivery.
No production imports, environment configuration, HTTP or Telegram client.
"""
from dataclasses import asdict
from decimal import Decimal
from html import escape
import json
import hashlib
import sqlite3
from urllib.parse import urlparse
from .prototype import normalize, matches, Car
from .price_review import review
from .eligibility import review_listing
from .fx import normalize_price, display_price
from .market import estimate_usd
from .source_dates import review_newness


def canonical(raw, now, first_seen=None, *, fx_quote=None):
    usd = normalize_price(raw, fx_quote, now)
    normalized_input = dict(raw)
    if usd.get('original_amount') is not None:
        normalized_input['price'] = usd['original_amount']
    c = asdict(normalize(normalized_input, now, first_seen=first_seen, source=raw.get('source', 'olx')))
    for key in ('generation', 'body', 'locality', 'engine_cc', 'created_at', 'price_kind', 'vehicle_key', 'checked_at'):
        c[key] = raw.get(key)
    location_date = raw.get('observed_location_date')
    c['observed_location_date'] = location_date.strip() if isinstance(location_date, str) and 0 < len(location_date) <= 500 else None
    search_reason = raw.get('observed_search_reason')
    c['observed_search_reason'] = search_reason if search_reason in ('promoted', 'organic') else None
    c['photos'] = [p for p in c['photos'] if urlparse(p).scheme == 'https']
    c['evidence'] = raw.get('evidence', 'unverified')
    price_input = dict(raw)
    if 'price_context' not in price_input and isinstance(raw.get('description'), str):
        price_input['price_context'] = (raw.get('title') or '') + '\n' + raw['description']
    c['price_review'] = review(price_input)
    c['eligibility_review'] = review_listing(raw)
    c['usd_price'] = usd
    for key in ('generation','body','locality','vehicle_key'):
        value=c[key]
        c[key]=value.strip() if isinstance(value,str) and value.strip() else None
    for key in ('engine_cc','created_at','checked_at'):
        value=c[key]
        c[key]=value if type(value) is int and value>0 else None
    return c


def filtered(c, f):
    fields = {k: c[k] for k in Car.__dataclass_fields__}
    # Seller amount remains unchanged in storage. Budgets use the USD view.
    if c.get('source') == 'olx':
        usd = c.get('usd_price', {})
        fields['price'] = usd.get('usd_amount') if usd.get('status') == 'ready' else None
        fields['currency'] = 'USD' if fields['price'] is not None else None
    base = Car(**fields)
    result = matches(base, f)
    if result is not True:
        return result
    wanted = f.get('body')
    if wanted and c['body'] is not None:
        if c['body'].casefold() not in [x.casefold() for x in (wanted if isinstance(wanted, list) else [wanted])]:
            return False
    return True


def estimate(target, comparables, now, *, minimum=8, max_age=30*86400):
    """OLX research method; no production AUTO.RIA imports or formula changes."""
    if target.get('detail_refresh_required'):
        return {'status': 'profitability_unconfirmed', 'reason': 'detail_refresh_required',
                'reasons': list(target.get('detail_refresh_reasons', [])), 'sample': 0,
                'currency': 'USD', 'method': 'lower_quartile', 'reference_usd': None,
                'range_usd': None, 'discount_percent': None, 'reliability': 'insufficient',
                'real_world_accuracy_verified': False, 'exclusions': {},
                'next_action': 'Obtain a fresh matching detail before valuation or delivery.'}
    return estimate_usd(target, comparables, now, minimum=minimum, max_age=max_age)


def _review_detail_newness(car, boundary, now):
    """Apply only a negative source-date gate; never establish publication.

    Reported creation predating the launch boundary, conflicting dates and
    wrong identities are held separately from trusted publication history.
    Missing dates/state and an apparently recent createdTime stay unknown;
    they cannot authorize delivery but do not erase independent prior proof.
    The persisted launch boundary is mandatory for a new review.
    """
    if car.get('source') != 'olx' or 'source_date_observations' not in car:
        return False
    previous_evidence = car.get('newness_negative_evidence', {})
    unresolved_previous = isinstance(previous_evidence, dict) and previous_evidence.get('review', {}).get('negative_hold') is True
    if boundary is None:
        if unresolved_previous:
            # Reuse an existing dated review; never invent a new boundary.
            car['newness_review'] = dict(previous_evidence['review'])
            car['newness_review']['reasons'] = sorted(set(car['newness_review'].get('reasons', [])) | {'previous_negative_evidence_unresolved'})
            return True
        return bool(car.get('newness_review', {}).get('negative_hold'))
    observations = car['source_date_observations']
    result = review_newness(observations, boundary=boundary, now=now)
    issues = observations.get('issues', []) if isinstance(observations, dict) else []
    # A missing state node is unavailable evidence. Present but malformed,
    # ambiguous or wrong-identity state is contradictory evidence and held.
    invalid = bool(issues) and issues != ['source_dates_unavailable']
    if isinstance(observations, dict) and observations.get('values') and not observations.get('identity_matches'):
        invalid = True
    result['negative_hold'] = result['status'] == 'reported_preexisting' or invalid
    result['boundary'] = boundary
    if unresolved_previous and not result['negative_hold']:
        # Unavailable or apparently recent source dates are not an audited
        # resolution of previously observed preexisting/conflicting evidence.
        result['negative_hold'] = True
        result['reasons'] = sorted(set(result['reasons']) | {'previous_negative_evidence_unresolved'})
    if result['negative_hold'] and not unresolved_previous:
        # Keep the first negative proof, bounded and without recursive history.
        car['newness_negative_evidence'] = {'review': dict(result),
                                           'source_date_observations': observations}
    car['newness_review'] = result
    return result['negative_hold']


def fingerprint(c):
    # Observation time may advance without changing the car or its price.
    # A thin search observation never refreshes the trusted detail evidence.
    # Its conflicting values are represented by the semantic refresh gate below.
    semantic = {k:v for k,v in c.items() if k not in ('checked_at','first_seen_at','updated_at','bumped_at','search_provenance','observed_location_date','observed_search_reason')}
    return hashlib.sha256(json.dumps(semantic,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _merge_search_refresh(previous, incoming, now):
    """Preserve parser-derived detail evidence under a thin HTML search refresh.

    The search card may display a conversion of the seller's asking currency.
    Neither equality across currencies nor a current full price is inferred.
    Retained details keep their original checked_at and therefore still expire.
    This boundary applies only to observed HTML, not generic fixture updates.
    """
    provenance = previous.get('detail_provenance')
    if (incoming.get('source') != 'olx'
            or incoming.get('evidence') != 'observed_search_html'
            or not isinstance(provenance, dict)
            or type(provenance.get('fetched_at')) is not int
            or previous.get('evidence') != 'observed_detail_html'):
        return incoming, False
    observed_at = incoming.get('checked_at')
    if type(observed_at) is not int or not 0 < observed_at <= now:
        raise ValueError('Explicit non-future search observation time required')
    old_search = previous.get('search_provenance', {})
    if not isinstance(old_search, dict):
        old_search = {}
    latest = max(provenance['fetched_at'], previous.get('checked_at') or 0,
                 old_search.get('observed_at', 0))
    if observed_at < latest:
        return previous, False

    merged = dict(previous)
    # Allowlisted public card facts only. The detail and search clocks remain
    # separate; no missing search field clears an observed detail fact.
    observed = {key: incoming.get(key) for key in
                ('url', 'title', 'price', 'currency', 'year', 'mileage_km',
                 'engine_cc', 'fuel', 'transmission', 'observed_location_date',
                 'observed_search_reason')}
    merged['search_provenance'] = {'observed_at': observed_at,
                                   'evidence': 'observed_search_html',
                                   'observation': observed}
    reasons = set(previous.get('detail_refresh_reasons', []))
    if incoming.get('price') is None or incoming.get('currency') is None:
        reasons.add('search_price_unavailable')
    elif previous.get('price') is None or previous.get('currency') is None:
        reasons.add('detail_price_unavailable')
    elif incoming['currency'] != previous['currency']:
        reasons.add('search_display_currency_differs')
    elif Decimal(incoming['price']) != Decimal(previous['price']):
        reasons.add('search_price_changed')
    for key in ('url', 'title', 'year', 'mileage_km', 'engine_cc', 'fuel', 'transmission'):
        if (incoming.get(key) is not None and previous.get(key) is not None
                and incoming[key] != previous[key]):
            reasons.add('search_' + key + '_changed')
    placement = {'observed_location_date', 'observed_search_reason'}
    old_observation = old_search.get('observation', {})
    old_semantic = {k: v for k, v in old_observation.items() if k not in placement}
    new_semantic = {k: v for k, v in observed.items() if k not in placement}
    if old_search.get('observed_at') == observed_at and old_semantic != new_semantic:
        reasons.add('same_time_conflicting_search_observations')
    if reasons:
        merged['detail_refresh_required'] = True
        merged['detail_refresh_reasons'] = sorted(reasons)
        # Retain the old asking amount and other details as dated history;
        # forbid valuation and pending delivery until a fresh detail is parsed.
        old_review = previous.get('price_review', {})
        merged['price_review'] = dict(old_review, status='needs_review',
            reasons=sorted(set(old_review.get('reasons', [])) | {'detail_refresh_required'}))
        merged['issues'] = tuple(sorted(set(previous.get('issues', [])) | {'detail_refresh_required'}))
    return merged, bool(reasons)


def source_selection(value=None):
    choices = {'AUTO.RIA': ('auto_ria',), 'OLX': ('olx',), 'Обидва джерела': ('auto_ria','olx')}
    return choices[value] if value is not None else ('auto_ria',)


def message(c):
    usd = c.get('usd_price', {})
    amount = usd.get('usd_amount')
    price = display_price(usd)
    fields = ['🚘 ' + (c.get('title') or 'Автомобіль'), price]
    if usd.get('status') == 'ready' and usd.get('original_currency') == 'UAH':
        fields.append(f"{usd['original_amount']} грн · довідковий перерахунок НБУ")
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

    def collect(self, fetch, now, *, page_budget, row_budget, fx_quote=None):
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
                source_complete = page.get('collection_complete', True)
                if type(source_complete) is not bool:
                    raise ValueError('collection_complete must be an explicit boolean')
                page_complete = page.get('page_complete', True)
                if type(page_complete) is not bool:
                    raise ValueError('page_complete must be an explicit boolean')
                terminal_incomplete = next_cursor is None and not source_complete
                resume_current_page = not page_complete or terminal_incomplete
                if rows + len(cards) > row_budget:
                    reason = 'row_budget'; break
                records = []
                refresh_required = []
                for raw in cards:
                    if not isinstance(raw,dict):
                        raise ValueError('invalid listing contract')
                    source = raw.get('source','olx')
                    old = self.db.execute('SELECT payload,eligibility FROM listings WHERE source=? AND id=?', (source,raw.get('id'))).fetchone()
                    previous = json.loads(old[0]) if old else None
                    c = canonical(raw, now, previous['first_seen_at'] if previous else None, fx_quote=fx_quote)
                    if previous:
                        c, needs_refresh = _merge_search_refresh(previous, c, now)
                        if needs_refresh:
                            refresh_required.append((source, c['id']))
                    eligible = ('new' if boundary <= c['published_at'] <= now else 'baseline') if c['publication_verified'] else 'unverified'
                    if old and old[1] in ('new', 'baseline'):
                        eligible = old[1]
                    records.append((source,c['id'],json.dumps(c),eligible))
                with self.db:
                    # An incomplete response must be fetched again at this
                    # cursor, even if a next link was already visible. Do not
                    # advance past cards lost in the unread tail of the body.
                    if not resume_current_page:
                        self.db.execute('INSERT INTO page_cursors VALUES(?)',(json.dumps(cursor),))
                    self.db.executemany('INSERT INTO listings VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload,eligibility=excluded.eligibility', records)
                    self.db.executemany("UPDATE deliveries SET status='needs_revalidation' WHERE source=? AND id=? AND status='pending'", refresh_required)
                    completed = next_cursor is None and source_complete and page_complete
                    saved_cursor = cursor if resume_current_page else next_cursor
                    self.db.execute('UPDATE checkpoint SET cursor=?,complete=?,last_success=?,retry_at=0,attempts=0', (saved_cursor,int(completed),now if completed else last))
                attempts = 0
                rows += len(cards); pages += 1; cursor = next_cursor
                if resume_current_page:
                    reason = 'page_incomplete' if not page_complete else 'source_incomplete'
                    break
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

    def apply_detail_snapshot(self, data, *, expected_id, expected_url, fetched_at,
                              truncated, fx_quote=None):
        """Parse raw detail bytes at the trusted offline boundary, then persist.

        Never accept an externally supplied 'allowed' decision as evidence. The
        parser reviews the complete visible description in memory; only redacted
        reasons and fingerprints are saved. HTTP and live delivery are absent.
        Existing first discovery/publication evidence survives a detail refresh;
        a new detail alone never establishes a new publication.
        """
        from .detail_snapshot import parse_detail_snapshot
        from .html_snapshot import clean_url
        if not isinstance(data, bytes) or not isinstance(expected_id, str):
            raise ValueError('Raw bytes and explicit expected listing identity required')
        url = clean_url(expected_url) if isinstance(expected_url, str) else None
        if not url or not expected_id.isdecimal():
            raise ValueError('Expected public OLX URL and numeric ID required')
        result = parse_detail_snapshot(data, fetched_at=fetched_at, truncated=truncated, fx_quote=fx_quote)
        car = result['listing']
        if car['source'] != 'olx' or car['id'] != expected_id or car['url'] != url:
            raise ValueError('Detail identity differs from requested listing')
        old = self.db.execute('SELECT payload,eligibility FROM listings WHERE source=? AND id=?', ('olx', expected_id)).fetchone()
        eligibility = 'unverified'
        if old:
            previous = json.loads(old[0])
            search_at = previous.get('search_provenance', {}).get('observed_at', 0)
            if max(previous.get('checked_at') or 0, search_at) > fetched_at:
                raise ValueError('An older detail must not replace a newer observation')
            car['first_seen_at'] = previous['first_seen_at']
            eligibility = old[1]
            if previous.get('publication_verified'):
                car['published_at'] = previous['published_at']
                car['publication_verified'] = True
                car['issues'] = tuple(x for x in car['issues'] if x != 'publication_unverified')
                car['publication_evidence_origin'] = 'preserved_previous_verified_record'
            if previous.get('search_provenance'):
                car['search_provenance'] = previous['search_provenance']
            if previous.get('newness_negative_evidence'):
                car['newness_negative_evidence'] = previous['newness_negative_evidence']
            elif previous.get('newness_review', {}).get('negative_hold'):
                # Compatibility with a negative review saved before the
                # immutable first-proof field was introduced.
                car['newness_negative_evidence'] = {
                    'review': previous['newness_review'],
                    'source_date_observations': previous.get('source_date_observations')}
        car['detail_provenance'] = {
            'body_sha256': hashlib.sha256(data).hexdigest(),
            'bytes_parsed': len(data),
            'download_truncated': result['summary']['download_truncated'],
            'description_complete': car['eligibility_review']['description_complete'],
            'fetched_at': fetched_at,
        }
        boundary = self.db.execute('SELECT boundary FROM checkpoint WHERE id=1').fetchone()[0]
        negative_hold = _review_detail_newness(car, boundary, fetched_at)
        with self.db:
            self.db.execute('INSERT INTO listings VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload,eligibility=excluded.eligibility',
                            ('olx', expected_id, json.dumps(car), eligibility))
            if negative_hold:
                self.db.execute("UPDATE deliveries SET status='needs_revalidation' WHERE source=? AND id=? AND status='pending'",
                                ('olx', expected_id))
        return {'stored': True, 'eligibility': eligibility,
                'offer_review': car['eligibility_review']['status'],
                'newness_review': car.get('newness_review'),
                'summary': result['summary']}

    def _persist_current_newness(self, car, boundary, now):
        """Refresh a saved source-date gate once a real launch boundary exists."""
        before = car.get('newness_review')
        held = _review_detail_newness(car, boundary, now)
        if car.get('newness_review') != before:
            self.db.execute('UPDATE listings SET payload=? WHERE source=? AND id=?',
                            (json.dumps(car), car['source'], car['id']))
        changed = 0
        if held:
            changed = self.db.execute("UPDATE deliveries SET status='needs_revalidation' WHERE source=? AND id=? AND status='pending'",
                                     (car['source'], car['id'])).rowcount
        return held, changed

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
        boundary = self.db.execute('SELECT boundary FROM checkpoint WHERE id=1').fetchone()[0]
        # Details may have been saved before the launch boundary existed.
        # Review those observations now without promoting their classification.
        for payload, eligibility in self.db.execute('SELECT payload,eligibility FROM listings').fetchall():
            c = json.loads(payload)
            newness_held, invalidated = self._persist_current_newness(c, boundary, now)
            pending -= invalidated
            if eligibility != 'new':
                continue
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
                if newness_held or match is None or assessment['status'] != 'experimental_estimate':
                    counts['uncertain'] += 1; continue
                if Decimal(str(assessment['discount_percent'])) < Decimal(str(u.get('min_discount',15))):
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
        boundary = self.db.execute('SELECT boundary FROM checkpoint WHERE id=1').fetchone()[0]
        for uid,source,id in self.db.execute("SELECT uid,source,id FROM deliveries WHERE status='pending'").fetchall():
            if source=='olx' and not olx_enabled:
                continue
            if not access(uid):
                counts['denied'] += 1; continue
            key=(uid,source,id)
            c=json.loads(self.db.execute('SELECT payload FROM listings WHERE source=? AND id=?',(source,id)).fetchone()[0])
            newness_held, _ = self._persist_current_newness(c, boundary, now)
            proof=self.db.execute('SELECT fingerprint,expires FROM delivery_proof WHERE uid=? AND source=? AND id=?',key).fetchone()
            from datetime import datetime
            from zoneinfo import ZoneInfo
            fx = c.get('usd_price', {}).get('fx')
            fx_current = not fx or fx.get('effective_date') == datetime.fromtimestamp(now, ZoneInfo('Europe/Kyiv')).date().isoformat()
            if (proof is None or proof[0] != fingerprint(c) or now > proof[1]
                    or newness_held
                    or c.get('detail_refresh_required')
                    or c.get('eligibility_review', {}).get('status') != 'allowed'
                    or c.get('usd_price', {}).get('status') != 'ready' or not fx_current):
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
