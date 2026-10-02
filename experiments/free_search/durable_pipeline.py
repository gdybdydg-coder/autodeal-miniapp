"""Durable offline continuation of the original parsers and filter experiment.

No production imports, environment settings, network client or real sender.
Loader/estimator/sender callbacks must be fixtures under OutboundGuard.
"""
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from experiments.free_search.filter_gate import PublicEvidence, SearchFilter, Span, gate
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.network_guard import OutboundGuard

MARKER = 0x46525032


def _json(value):
    return json.dumps(value, default=lambda x: str(x) if isinstance(x, Decimal) else list(x), sort_keys=True, allow_nan=False)


def _time(value):
    if type(value) not in (float,int) or not math.isfinite(value) or value <= 0:
        raise ValueError('invalid_time')
    return float(value)


def _reason(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z0-9_]{1,100}',value):
        raise ValueError('invalid_reason')
    return value


def _filters(value):
    data=json.loads(value)
    data['price_usd']=Span(**{k:None if v is None else Decimal(v) for k,v in data['price_usd'].items()})
    data['min_discount_percent']=Decimal(data['min_discount_percent'])
    for field in ('regions','bodies','fuels','transmissions'):
        data[field]=frozenset(data[field])
    return SearchFilter(**data)


def _evidence(details,published):
    data={k:details[k] for k in PublicEvidence.__dataclass_fields__ if k in details}
    data['listing_id']=str(details['listing_id'])
    data['publication_proven']=published is not None and details.get('publication_proven',True) is True
    price=details.get('price_usd')
    if price is None and details.get('currency')=='USD':
        price=details.get('price')
    data['price_usd']=None if price is None else Decimal(str(price))
    for key in ('year','mileage_km'):
        if data.get(key) is not None:
            try:
                value=Decimal(str(data[key]))
                data[key]=int(value) if value.is_finite() and value>=0 and value==value.to_integral_value() else None
            except (ValueError,ArithmeticError):
                data[key]=None
    # Public JSON-LD sometimes uses the broad vehicle category as bodyType.
    if data.get('body') in ('Легкові','Легковые'):
        data['body']=None
    return PublicEvidence(**data)


class DurablePipeline:
    """Single-process SQLite replay, bounded active work, archived unresolved cases.

    Whole pages persist together with their cursor, or the cursor does not move.
    A measured overlap floor is mandatory; no hidden production default exists.
    Listing details and estimates are shared; delivery dedupes by (car,user).
    """
    def __init__(self,path,*,max_pending=5000,max_attempts=3,retry_seconds=5):
        path=Path(path)
        if not path.name.endswith('.free-pipeline.sqlite3') or path.is_symlink():
            raise ValueError('experiment_database_required')
        if any(type(n) is not int or n<=0 for n in (max_pending,max_attempts,retry_seconds)):
            raise ValueError('invalid_policy')
        existed=path.exists()
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        if existed and self.db.execute('PRAGMA application_id').fetchone()[0]!=MARKER:
            self.db.close()
            raise ValueError('foreign_database')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.max_pending,self.max_attempts,self.retry_seconds=max_pending,max_attempts,retry_seconds
        self.guard=OutboundGuard()
        self.db.executescript(f'''
            PRAGMA application_id={MARKER};
            CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,observed REAL NOT NULL,
              floor REAL NOT NULL,next_page INTEGER NOT NULL DEFAULT 1,page_budget INTEGER NOT NULL,
              state TEXT NOT NULL,reason TEXT,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(scan TEXT NOT NULL,page INTEGER NOT NULL,
              fingerprint TEXT NOT NULL,PRIMARY KEY(scan,page));
            CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY,url TEXT,
              published REAL,discovered REAL NOT NULL,stage TEXT NOT NULL,
              due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,details TEXT,estimate TEXT,
              valued REAL,reason TEXT NOT NULL,detail_calls INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS listing_history(id INTEGER PRIMARY KEY,listing_id TEXT NOT NULL,at REAL NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recipients(user_id INTEGER NOT NULL,search_id INTEGER NOT NULL,
              enabled INTEGER NOT NULL,access_until REAL,access_known INTEGER NOT NULL,
              started REAL NOT NULL,filters TEXT NOT NULL,PRIMARY KEY(user_id,search_id));
            CREATE TABLE IF NOT EXISTS claims(listing_id TEXT NOT NULL,user_id INTEGER NOT NULL,
              state TEXT NOT NULL,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
              method TEXT NOT NULL,reason TEXT NOT NULL,accepted REAL,
              PRIMARY KEY(listing_id,user_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL NOT NULL,
              stage TEXT NOT NULL,reason TEXT NOT NULL,listing_id TEXT);
            CREATE INDEX IF NOT EXISTS listing_due ON listings(stage,due);
            CREATE INDEX IF NOT EXISTS claim_due ON claims(state,due);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def _tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _event(self,now,stage,reason,sid=None):
        self.db.execute('INSERT INTO events(at,stage,reason,listing_id) VALUES (?,?,?,?)',(_time(now),_reason(stage),_reason(reason),sid))

    def start_scan(self,scan_id,observed_at,floor_at,*,page_budget=20):
        _time(observed_at); _time(floor_at)
        if floor_at>observed_at or type(page_budget) is not int or not 1<=page_budget<=1000:
            raise ValueError('invalid_scan')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',scan_id):
            raise ValueError('invalid_scan')
        with self._tx():
            self.db.execute('INSERT OR IGNORE INTO scans(id,observed,floor,page_budget,state,due) VALUES (?,?,?,?,?,?)',(scan_id,observed_at,floor_at,page_budget,'scanning',observed_at))

    def ingest_html_page(self,scan_id,page,html,*,has_more,now):
        cards=parse_public_cards(html)
        candidates=[dict(listing_id=c.listing_id,url=c.url,published_at=c.added_at,promoted=c.promoted,publication_issues=list(c.issues)) for c in cards]
        return self.ingest_page(scan_id,page,candidates,has_more=has_more,now=now)

    def ingest_page(self,scan_id,page,candidates,*,has_more,now):
        _time(now)
        if type(page) is not int or page<1 or type(has_more) is not bool:
            raise ValueError('invalid_page')
        normalized=[]
        for raw in candidates:
            sid=str(raw['listing_id'])
            if not re.fullmatch(r'[1-9][0-9]{0,11}',sid):
                raise ValueError('invalid_listing_id')
            published=raw.get('published_at')
            if published is not None:
                _time(published)
            normalized.append((sid,raw.get('url'),published,bool(raw.get('promoted')),tuple(raw.get('publication_issues',()))))
        if len({r[0] for r in normalized})!=len(normalized):
            raise ValueError('duplicate_page_id')
        fingerprint=hashlib.sha256(_json(normalized).encode()).hexdigest()
        with self._tx():
            scan=self.db.execute('SELECT * FROM scans WHERE id=?',(scan_id,)).fetchone()
            if scan is None:
                raise ValueError('scan_missing')
            existing=self.db.execute('SELECT * FROM pages WHERE scan=? AND page=?',(scan_id,page)).fetchone()
            if existing:
                if existing['fingerprint']!=fingerprint:
                    return dict(accepted=False,queued=0,reason='page_changed_rescan_required')
                return dict(accepted=True,queued=0,reason='already_persisted')
            if now<scan['due']:
                return dict(accepted=False,queued=0,reason='source_backoff')
            if page!=scan['next_page'] or scan['state'] not in ('scanning','overflow'):
                raise ValueError('page_out_of_sequence')
            if self.db.execute('SELECT 1 FROM pages WHERE scan=? AND fingerprint=?',(scan_id,fingerprint)).fetchone():
                self.db.execute("UPDATE scans SET state='incomplete',reason='repeated_page' WHERE id=?",(scan_id,))
                return dict(accepted=False,queued=0,reason='repeated_page')
            inserts=[]
            replacements=[]
            replacement_capacity=0
            for sid,url,published,promoted,issues in normalized:
                existing_listing=self.db.execute('SELECT * FROM listings WHERE id=?',(sid,)).fetchone()
                if existing_listing is not None:
                    old_publication=existing_listing['published']
                    newer=published is not None and (old_publication is None or published>old_publication)
                    evidence_recovered=(existing_listing['reason']=='publication_not_proven' and not issues and published is not None)
                    if not newer and not evidence_recovered:
                        continue
                stage,reason='detail','discovered'
                if published is None or set(issues)-{'invalid_update_date','invalid_or_conflicting_preview_price'}:
                    stage,reason='unresolved','publication_not_proven'
                elif published>scan['observed']:
                    stage,reason='unresolved','publication_in_future'
                elif published<=scan['floor']:
                    stage,reason='excluded','outside_recent_window'
                elif promoted:
                    stage,reason='unresolved','promoted_publication_uncertain'
                if existing_listing is None:
                    inserts.append((sid,url,published,now,stage,now,reason))
                elif stage=='detail':
                    replacements.append((existing_listing,sid,url,published,now))
                    if existing_listing['stage'] not in ('detail','loading','valuation','evaluating'):
                        replacement_capacity+=1
            active=self.db.execute("SELECT COUNT(*) FROM listings WHERE stage IN ('detail','loading','valuation','evaluating')").fetchone()[0]
            if active+sum(r[4]=='detail' for r in inserts)+replacement_capacity>self.max_pending:
                self.db.execute("UPDATE scans SET state='overflow',reason='queue_capacity' WHERE id=?",(scan_id,))
                self._event(now,'discovery','queue_capacity')
                return dict(accepted=False,queued=0,reason='queue_capacity')
            self.db.executemany('INSERT INTO listings(id,url,published,discovered,stage,due,reason) VALUES (?,?,?,?,?,?,?)',inserts)
            for old,sid,url,published,observed in replacements:
                self.db.execute('INSERT INTO listing_history(listing_id,at,reason,payload) VALUES (?,?,?,?)',(sid,now,'publication_reobserved',_json(dict(old))))
                self.db.execute("UPDATE listings SET url=?,published=?,discovered=?,stage='detail',due=?,attempts=0,details=NULL,estimate=NULL,valued=NULL,reason='publication_reobserved' WHERE id=?",(url,published,observed,observed,sid))
            self.db.execute('INSERT INTO pages VALUES (?,?,?)',(scan_id,page,fingerprint))
            state='complete' if not has_more else ('incomplete' if page>=scan['page_budget'] else 'scanning')
            reason='source_end_observed' if not has_more else ('page_budget_reached' if state=='incomplete' else 'next_page_required')
            self.db.execute('UPDATE scans SET next_page=?,state=?,reason=?,attempts=0 WHERE id=?',(page+1,state,reason,scan_id))
            self._event(now,'discovery',reason)
            return dict(accepted=True,queued=sum(r[4]=='detail' for r in inserts)+len(replacements),reason=reason)

    def record_scan_failure(self,scan_id,reason,now,*,retry_after=None):
        _reason(reason); _time(now)
        with self._tx():
            row=self.db.execute('SELECT * FROM scans WHERE id=?',(scan_id,)).fetchone()
            if row is None:
                raise ValueError('scan_missing')
            attempts=row['attempts']+1
            state='unresolved' if attempts>=self.max_attempts or reason in ('denied','captcha') else row['state']
            delay=max(self.retry_seconds*2**(attempts-1),retry_after or 0)
            self.db.execute('UPDATE scans SET state=?,reason=?,attempts=?,due=? WHERE id=?',(state,reason,attempts,now+delay,scan_id))
            self._event(now,'source',reason)

    def put_recipient(self,user_id,search_id,filters,*,started_at,access_until,enabled=True,access_known=True):
        if type(user_id) is not int or user_id<=0 or type(search_id) is not int or search_id<=0:
            raise ValueError('invalid_recipient')
        _time(started_at)
        if access_until is not None:
            _time(access_until)
        if not isinstance(filters,SearchFilter):
            raise ValueError('invalid_filter')
        with self._tx():
            self.db.execute('INSERT OR REPLACE INTO recipients VALUES (?,?,?,?,?,?,?)',(user_id,search_id,int(enabled),access_until,int(access_known),started_at,_json(asdict(filters))))

    def stop_user(self,user_id):
        with self._tx():
            self.db.execute('UPDATE recipients SET enabled=0 WHERE user_id=?',(user_id,))
            self.db.execute("UPDATE claims SET state='cancelled',reason='stopped' WHERE user_id=? AND state='pending'",(user_id,))

    def set_access(self,user_id,access_until,*,known=True):
        """An access grant never resets an explicit /stop."""
        if access_until is not None:
            _time(access_until)
        with self._tx():
            self.db.execute('UPDATE recipients SET access_until=?,access_known=? WHERE user_id=?',(access_until,int(known),user_id))

    def _retry_listing(self,row,stage,now,reason):
        attempts=row['attempts']
        final='unresolved' if attempts>=self.max_attempts else stage
        self.db.execute('UPDATE listings SET stage=?,due=?,reason=? WHERE id=?',(final,now+self.retry_seconds*2**max(0,attempts-1),reason,row['id']))
        self._event(now,stage,reason,row['id'])

    def process_details(self,loader,now,*,limit=1000):
        """loader(listing_row_dict) -> normalized details dict; fixtures only."""
        _time(now)
        ids=[r[0] for r in self.db.execute("SELECT id FROM listings WHERE stage='detail' AND due<=? ORDER BY discovered,id LIMIT ?",(now,limit))]
        for sid in ids:
            with self._tx():
                self.db.execute("UPDATE listings SET stage='loading',attempts=attempts+1,detail_calls=detail_calls+1 WHERE id=?",(sid,))
            row=self.db.execute('SELECT * FROM listings WHERE id=?',(sid,)).fetchone()
            try:
                with self.guard.isolated():
                    details=dict(loader(dict(row)))
                if str(details.get('listing_id'))!=sid:
                    raise ValueError('detail_id_conflict')
                observed=_time(details.get('observed_at'))
                if observed>now or observed<row['discovered']:
                    raise ValueError('detail_observation_not_current')
                evidence=_evidence(details,row['published'])
                if details.get('price_usd') is not None and details.get('currency')=='USD' and details.get('price') is not None and Decimal(str(details['price']))!=evidence.price_usd:
                    raise ValueError('detail_price_conflict')
                if evidence.price_usd is None or not evidence.price_usd.is_finite() or evidence.price_usd<=0:
                    raise ValueError('current_price_unavailable')
                details.update(asdict(evidence))
                payload=_json(details)
            except Exception:
                with self._tx():
                    self._retry_listing(row,'detail',now,'detail_unavailable_or_invalid')
                continue
            with self._tx():
                self.db.execute("UPDATE listings SET stage='valuation',details=?,attempts=0,due=?,reason='detail_ready' WHERE id=?",(payload,now,sid))
                self._event(now,'detail','detail_ready',sid)
        return len(ids)

    def _qualifies(self,recipient,listing,now):
        if not recipient['access_known']:
            return False,'access_unknown'
        if not recipient['enabled']:
            return False,'stopped'
        if recipient['access_until'] is None or recipient['access_until']<=now:
            return False,'access_expired'
        if listing['published'] is None or recipient['started']>listing['published']:
            return False,'before_search_start'
        filters=_filters(recipient['filters'])
        evidence=_evidence(json.loads(listing['details']),listing['published'])
        result=gate(evidence,filters)
        if not result.eligible_for_valuation:
            return False,result.blockers[0]
        estimate=json.loads(listing['estimate'])
        reference=Decimal(estimate['reference_price_usd'])
        discount=(reference-evidence.price_usd)/reference*100
        return (True,'eligible') if discount>=filters.min_discount_percent else (False,'discount_mismatch')

    def process_valuations(self,estimator,now,*,limit=1000):
        """estimator(details,now) -> status,reference_price_usd,reason,provenance.

        Only experimental_peer_asking_v1 is accepted, never AUTO.RIA lower*0.95.
        The user discount threshold is evaluated separately for each recipient.
        """
        _time(now)
        ids=[r[0] for r in self.db.execute("SELECT id FROM listings WHERE stage='valuation' AND due<=? ORDER BY discovered,id LIMIT ?",(now,limit))]
        for sid in ids:
            with self._tx():
                self.db.execute("UPDATE listings SET stage='evaluating',attempts=attempts+1 WHERE id=?",(sid,))
            row=self.db.execute('SELECT * FROM listings WHERE id=?',(sid,)).fetchone()
            try:
                with self.guard.isolated():
                    result=dict(estimator(json.loads(row['details']),now))
                reason=_reason(result.get('reason','valuation_unknown'))
                if result.get('status')!='estimated' or result.get('classification') in ('suspicious_price','insufficient_data'):
                    raise LookupError(reason)
                reference=Decimal(str(result['reference_price_usd']))
                if not reference.is_finite() or reference<=0 or result.get('provenance')!='experimental_peer_asking_v1':
                    raise ValueError('unapproved_reference')
                result['reference_price_usd']=str(reference)
                payload=_json(result)
            except Exception as error:
                reason=str(error) if isinstance(error,LookupError) else 'valuation_unavailable_or_invalid'
                try:
                    _reason(reason)
                except ValueError:
                    reason='valuation_unavailable_or_invalid'
                with self._tx():
                    self._retry_listing(row,'valuation',now,reason)
                continue
            with self._tx():
                self.db.execute("UPDATE listings SET estimate=?,valued=?,stage='complete',reason='evaluated' WHERE id=?",(payload,now,sid))
                listing=self.db.execute('SELECT * FROM listings WHERE id=?',(sid,)).fetchone()
                self._build_claims(listing,now)
                self._event(now,'valuation','experimental_estimate_ready',sid)
        return len(ids)

    def _build_claims(self,listing,now):
        targets=set()
        unknown_targets={}
        details=json.loads(listing['details'])
        method='photo' if details.get('photo') else 'text'
        for recipient in self.db.execute('SELECT * FROM recipients ORDER BY user_id,search_id'):
            allowed,reason=self._qualifies(recipient,listing,now)
            if allowed or reason=='access_unknown':
                targets.add(recipient['user_id'])
            else:
                if 'not_proven' in reason:
                    unknown_targets[recipient['user_id']]=reason
                self._event(now,'filter',reason,listing['id'])
        for uid in set(unknown_targets)-targets:
            self.db.execute("INSERT OR IGNORE INTO claims(listing_id,user_id,state,due,method,reason) VALUES (?,?,'unresolved',?,?,?)",(listing['id'],uid,now,method,unknown_targets[uid]))
        for uid in targets:
            self.db.execute("INSERT OR IGNORE INTO claims(listing_id,user_id,state,due,method,reason) VALUES (?,?,'pending',?,?,'queued')",(listing['id'],uid,now,method))

    def send_due(self,sender,now,*,limit=100000,clock=None):
        """Fixture sender returns outcome tokens, not actual Telegram responses.

        accepted needs a positive synthetic message_id. Ambiguous exceptions
        stay uncertain. Known failures back off; entitlement is checked per call.
        """
        _time(now)
        keys=[tuple(r) for r in self.db.execute("SELECT listing_id,user_id FROM claims WHERE state='pending' AND due<=? ORDER BY due,listing_id,user_id LIMIT ?",(now,limit))]
        attempted=0
        for sid,uid in keys:
            send_at=_time(clock() if clock is not None else now)
            with self._tx():
                claim=self.db.execute('SELECT * FROM claims WHERE listing_id=? AND user_id=?',(sid,uid)).fetchone()
                if claim['state']!='pending':
                    continue
                listing=self.db.execute('SELECT * FROM listings WHERE id=?',(sid,)).fetchone()
                if listing['stage']!='complete':
                    self.db.execute('UPDATE claims SET due=?,reason=? WHERE listing_id=? AND user_id=?',(send_at+self.retry_seconds,'publication_recheck_pending',sid,uid))
                    continue
                recipients=list(self.db.execute('SELECT * FROM recipients WHERE user_id=?',(uid,)))
                checks=[self._qualifies(r,listing,send_at) for r in recipients]
                if not any(ok for ok,_ in checks):
                    reason='access_unknown' if any(reason=='access_unknown' for _,reason in checks) else 'recipient_ineligible'
                    if reason=='access_unknown':
                        count=claim['attempts']+1
                        state='unresolved' if count>=self.max_attempts else 'pending'
                        self.db.execute('UPDATE claims SET state=?,attempts=?,reason=?,due=? WHERE listing_id=? AND user_id=?',(state,count,reason,send_at+self.retry_seconds,sid,uid))
                    else:
                        self.db.execute("UPDATE claims SET state='cancelled',reason=? WHERE listing_id=? AND user_id=?",(reason,sid,uid))
                    continue
                self.db.execute("UPDATE claims SET state='sending',attempts=attempts+1 WHERE listing_id=? AND user_id=?",(sid,uid))
            claim=dict(self.db.execute('SELECT * FROM claims WHERE listing_id=? AND user_id=?',(sid,uid)).fetchone())
            attempted+=1
            try:
                with self.guard.isolated():
                    result=sender(claim,json.loads(listing['details']))
                if not isinstance(result,dict):
                    result={'outcome':'uncertain'}
            except Exception:
                result={'outcome':'uncertain'}
            outcome=result.get('outcome')
            with self._tx():
                state,reason,method,due='uncertain','ambiguous_delivery',claim['method'],send_at
                if outcome=='accepted' and type(result.get('message_id')) is int and result['message_id']>0:
                    state,reason='sent','telegram_accepted_simulated'
                elif outcome in ('rate_limited','known_transient','photo_invalid'):
                    state='unresolved' if claim['attempts']>=self.max_attempts else 'pending'
                    reason=outcome
                    try:
                        delay=float(result.get('retry_after',self.retry_seconds*2**(claim['attempts']-1)))
                        if not math.isfinite(delay) or delay<0:
                            raise ValueError()
                    except (ValueError,TypeError):
                        delay=86400
                    due=send_at+max(self.retry_seconds,delay)
                    if outcome=='photo_invalid':
                        method='text'
                elif outcome in ('blocked','rejected'):
                    state,reason='failed',outcome
                self.db.execute('UPDATE claims SET state=?,reason=?,method=?,due=?,accepted=? WHERE listing_id=? AND user_id=?',(state,reason,method,due,send_at if state=='sent' else None,sid,uid))
                self._event(send_at,'delivery',reason,sid)
        return attempted

    def recover(self,now):
        _time(now)
        with self._tx():
            rows=list(self.db.execute("SELECT * FROM listings WHERE stage IN ('loading','evaluating')"))
            for row in rows:
                self._retry_listing(row,'detail' if row['stage']=='loading' else 'valuation',now,'restart_interrupted')
            self.db.execute("UPDATE claims SET state='uncertain',reason='restart_send_uncertain' WHERE state='sending'")
            self._event(now,'restart','recovery_complete')

    def rows(self,table):
        if table not in ('scans','pages','listings','listing_history','recipients','claims','events'):
            raise ValueError('invalid_table')
        return [dict(row) for row in self.db.execute('SELECT * FROM '+table)]

    def summary(self,now):
        _time(now)
        listings={r['stage']:r['n'] for r in self.db.execute('SELECT stage,COUNT(*) n FROM listings GROUP BY stage')}
        claims={r['state']:r['n'] for r in self.db.execute('SELECT state,COUNT(*) n FROM claims GROUP BY state')}
        oldest=self.db.execute("SELECT MIN(discovered) FROM listings WHERE stage NOT IN ('complete','excluded')").fetchone()[0]
        return dict(listings=listings,claims=claims,unique_listings=sum(listings.values()),listing_user_pairs=sum(claims.values()),oldest_unprocessed_age_seconds=None if oldest is None else max(0,now-oldest),detail_calls=self.db.execute('SELECT COALESCE(SUM(detail_calls),0) FROM listings').fetchone()[0],paid_calls=self.guard.paid_calls,real_telegram_calls=self.guard.successful_external_calls,outbound_guard=self.guard.summary(),source_coverage_proven=False,production_ready=False)
