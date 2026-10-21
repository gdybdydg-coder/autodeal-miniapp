"""Restartable owner research intake, independent of the production monitor.

One HTTP request at a time through the existing ledger. Head reads and details
interleave; browser valuation is a separate consumer of persisted detail work.
This module has no sender, credentials, scheduler installation, or paid API.
"""
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from .owner_budget import Budget, Denied
from .owner_candidate_queue import CandidateQueue
from .owner_comparison_batch import collect_details
from .owner_head_probe import wait_for_fetch
from .owner_head_window import HeadWindow, HEAD
from .owner_night_runtime import Coordinator, LOCK, WORKSPACE
from .owner_observations import Observations
from .owner_public_fetch import PublicFetch, ROBOTS
from .owner_stage_pipeline import StagePipeline
from .public_cards import parse_public_cards
from .public_pagination import inspect_page
from .network_guard import CREDENTIAL_NAMES
from .owner_browser_outbox import BrowserOutbox


class IntakeState:
    def __init__(self, queue, *, pages=3, head_seconds=12):
        if type(pages) is not int or not 1 <= pages <= 6:
            raise ValueError('invalid_pages')
        if type(head_seconds) is not int or head_seconds < 8:
            raise ValueError('invalid_head_interval')
        self.db, self.pages, self.head_seconds = queue.db, pages, head_seconds
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS intake_control (
            id INTEGER PRIMARY KEY CHECK(id=1), pages INTEGER, head_seconds INTEGER,
            next_page INTEGER, next_head REAL, stopped INTEGER, reason TEXT);
          CREATE TABLE IF NOT EXISTS intake_previews (
            id TEXT PRIMARY KEY, price TEXT, observed_at REAL);
          CREATE TABLE IF NOT EXISTS intake_events (
            id INTEGER PRIMARY KEY, at REAL, kind TEXT, evidence TEXT);
        ''')
        self.db.execute('INSERT OR IGNORE INTO intake_control VALUES(1,?,?,1,0,0,NULL)',
                        (pages, head_seconds))
        if 'city' not in {r[1] for r in self.db.execute('PRAGMA table_info(intake_previews)')}:
            self.db.execute('ALTER TABLE intake_previews ADD COLUMN city TEXT')
        if self.db.execute('SELECT pages,head_seconds FROM intake_control').fetchone() != (pages, head_seconds):
            raise ValueError('intake_policy_changed')
        self.db.execute('CREATE TABLE IF NOT EXISTS intake_rotation (id INTEGER PRIMARY KEY CHECK(id=1), tail INTEGER NOT NULL, enabled INTEGER NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO intake_rotation VALUES(1,2,0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS intake_missing_pages (page INTEGER PRIMARY KEY, retry_after REAL NOT NULL)')

    def available_tail(self, start, now):
        if self.pages<2:return 1
        for offset in range(self.pages-1):
            page=2+(start-2+offset)%(self.pages-1)
            row=self.db.execute('SELECT retry_after FROM intake_missing_pages WHERE page=?',(page,)).fetchone()
            if not row or now>=row[0]:return page
        return 1

    def missing_head(self, page, now, receipt):
        # A vanished later page does not stop fresh-head discovery. The omission
        # remains explicit and the page receives a durable ten-minute cooldown.
        if page<=1 or page>self.pages or receipt.get('status')!=404:
            raise ValueError('invalid_missing_head_evidence')
        if self.db.execute('SELECT next_page FROM intake_control').fetchone()[0]!=page:
            raise ValueError('head_cursor_conflict')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO intake_missing_pages VALUES(?,?) ON CONFLICT(page) DO UPDATE SET retry_after=excluded.retry_after',(page,now+600))
            self.db.execute('UPDATE intake_rotation SET tail=?',(2 if page==self.pages else page+1,))
            self.db.execute('UPDATE intake_control SET next_page=1,next_head=?',(now+self.head_seconds,))
            self.record('missing_head',{'page':page,'receipt':receipt,'retry_after':now+600,'whole_market_recall':None},now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def expand(self, pages, now):
        """Explicit expansion only; retain receipts, first_seen, stop and cursor."""
        if type(pages) is not int or not self.pages <= pages <= 6:
            raise ValueError('invalid_coverage_expansion')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE intake_control SET pages=?', (pages,))
            self.db.execute('UPDATE intake_rotation SET enabled=1')
            self.record('coverage_expanded', {'before':self.pages,'after':pages,
                        'schedule':'head interleaved with rotating tail','whole_market_recall':None}, now)
            self.db.execute('COMMIT')
            self.pages=pages
        except BaseException:
            self.db.execute('ROLLBACK'); raise

    def record(self, kind, evidence, now):
        self.db.execute('INSERT INTO intake_events(at,kind,evidence) VALUES(?,?,?)',
                        (now, kind, json.dumps(evidence, default=str, allow_nan=False)))

    def stop(self, reason, now):
        self.db.execute('UPDATE intake_control SET stopped=1,reason=?', (reason,))
        self.record('stop', {'reason': reason}, now)

    def resume(self, now):
        # Explicit CLI action only; opening the database never clears a stop.
        self.db.execute('UPDATE intake_control SET stopped=0,reason=NULL')
        self.record('resume', {}, now)

    def action(self, now, detail_available):
        page, due, stopped = self.db.execute('SELECT next_page,next_head,stopped FROM intake_control').fetchone()
        if stopped:
            return 'stop', None
        if now >= due:
            return 'head', page
        return ('detail', None) if detail_available else ('wait', due)

    def accept_head(self, page, cards, now, evidence):
        expected = self.db.execute('SELECT next_page FROM intake_control').fetchone()[0]
        if page != expected:
            raise ValueError('head_cursor_conflict')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for card in cards:
                self.db.execute('INSERT INTO intake_previews(id,price,observed_at,city) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at,city=excluded.city',
                    (card.listing_id, str(card.preview_usd) if card.preview_usd is not None else None, now,getattr(card,'preview_city',None)))
            tail, interleave = self.db.execute('SELECT tail,enabled FROM intake_rotation').fetchone()
            if interleave and self.pages > 1:
                next_page = self.available_tail(tail,now) if page == 1 else 1
                if page != 1:
                    self.db.execute('UPDATE intake_rotation SET tail=?',(2 if page >= self.pages else page+1,))
            else:
                next_page = 1 if page == self.pages else page+1
            self.db.execute('UPDATE intake_control SET next_page=?,next_head=?',
                            (next_page, now+self.head_seconds))
            self.record('head', evidence, now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def previews(self):
        return {ident: SimpleNamespace(preview_usd=Decimal(price) if price is not None else None,preview_city=city)
                for ident, price, city in self.db.execute('SELECT id,price,city FROM intake_previews')}


def detail_priority(row, previews, searches):
    # Ordering hint only. Suburbs and unknown cities are NEVER filtered out here;
    # authoritative region/category still comes from the listing detail page.
    capitals={'Вінницька область':'Вінниця','Тернопільська область':'Тернопіль',
              'Хмельницька область':'Хмельницький','Чернівецька область':'Чернівці'}
    regions=set()
    for search in searches:
        region=search['filters'].get('region',[])
        regions.update([region] if isinstance(region,str) else region or [])
    preferred={capitals[r] for r in regions if r in capitals}
    preview=previews.get(row['id'])
    return (getattr(preview,'preview_city',None) not in preferred,row['first_seen'],row['id'])


def current_research_searches(path, now):
    """Research snapshot is never send authority. Expired snapshots block details."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if data.get('status') != 'readonly_ok' or any(data.get('access',{}).get(k) is not True for k in ('ready','entitled','approved')):
        return []
    checked = data.get('at')
    if (type(checked) not in (int,float) or not math.isfinite(checked)
            or not 0 <= now-checked <= 300):
        return []
    return [s for s in data.get('searches',[]) if s.get('enabled') is True]


def run_owned(token, deadline, *, max_requests=40, pages=3):
    """Run inside an already-owned daytime session; release remains caller's job."""
    if type(max_requests) is not int or not 1 <= max_requests <= 200:
        raise ValueError('invalid_request_bound')
    if not time.time() < deadline <= time.time()+2400:
        raise ValueError('invalid_deadline')
    if any(os.environ.get(k) for k in CREDENTIAL_NAMES):
        raise Denied('production_configuration_present')
    guard = Coordinator(LOCK)
    try:
        if not guard.owns(token):
            raise Denied('executor_lock_required')
        budget = Budget(WORKSPACE/'free-public-audit-20261020.sqlite3', 2**63-1, 2**63-1, allow_feed_pagination=True)
        queue = CandidateQueue(WORKSPACE/'night21-candidates.sqlite3')
        obs = Observations(WORKSPACE/'night-20261020.owner-observations.sqlite3')
        state = IntakeState(queue, pages=pages)
        pipeline, window, fetch = StagePipeline(queue,obs), HeadWindow(obs), PublicFetch(budget)
        outbox=BrowserOutbox(WORKSPACE/'continuous-browser-outbox')
        report = {'started_at':time.time(), 'budget_before':budget.summary(), 'receipts':[],
                  'details':[], 'tickets':[], 'valuations':[], 'paid_calls':0, 'telegram_calls':0,
                  'publication_at':None, 'coverage':'repeated first '+str(pages)+' public pages; whole-market recall unknown'}
        def get(url, cap):
            if not guard.owns(token):
                raise Denied('executor_lock_lost')
            if state.action(time.time(),False)[0] == 'stop':
                raise Denied('intake_stopped')
            if len(report['receipts']) >= max_requests:
                raise Denied('run_request_bound')
            budget.observe(url)
            wait_for_fetch(budget.db.execute('SELECT next_at FROM budget').fetchone()[0],deadline)
            if state.action(time.time(),False)[0] == 'stop':
                raise Denied('intake_stopped')
            body, receipt = fetch.get(url,cap)
            report['receipts'].append(receipt); state.record('request',receipt,time.time())
            if body is None and receipt.get('status')==404 and url!=ROBOTS:
                return None,receipt
            if body is None:
                raise Denied('source_unavailable_no_retry')
            return body, receipt
        try:
            if state.action(time.time(),False)[0] == 'stop':
                raise Denied('intake_stopped')
            get(ROBOTS,20000)
            while time.time()+21 < deadline and len(report['receipts']) < max_requests:
                pipeline.expire(time.time())
                searches = current_research_searches(WORKSPACE/'comparison-current-access-20261021.json',time.time())
                report['valuations'].extend(outbox.consume(pipeline,searches,time.time()))
                outbox.recover(pipeline,searches,time.time(),deadline)
                previews = state.previews()
                candidates = [row for row in queue.pending(100,stage='discovered') if row['id'] in previews]
                action, value = state.action(time.time(), bool(candidates and searches))
                if action == 'stop':
                    raise Denied('intake_stopped')
                if action == 'head':
                    url = HEAD+f'?page={value}'
                    body, receipt = get(url,2500000)
                    if body is None:
                        state.missing_head(value,receipt['completed_at'],receipt)
                        continue
                    # Bounded last-page artifact for parser audits, never delivery evidence.
                    (WORKSPACE/'continuous-last-head-20261021.html').write_text(body,encoding='utf-8')
                    cards = parse_public_cards(body); meta = inspect_page(body,url)
                    coverage = window.accept(meta,cards,receipt['completed_at'])
                    added = pipeline.ingest(cards,receipt['completed_at'])
                    state.accept_head(value,cards,receipt['completed_at'],
                        {'url':url, 'ids':[c.listing_id for c in cards], 'queued':added, 'coverage':coverage})
                elif action == 'detail':
                    before = len(report['receipts'])
                    collect_details(pipeline,previews,searches,get,report,deadline,1,
                        priority=lambda row:detail_priority(row,previews,searches),on_ticket=outbox.publish)
                    if before == len(report['receipts']):
                        # Price-preview mismatches cannot spin or starve head polling.
                        time.sleep(min(1,max(0,deadline-time.time())))
                else:
                    time.sleep(min(1,max(0,value-time.time())))
            report['reason'] = 'session_bound_reached'
        except ValueError as exc:
            report['reason'] = str(exc)
            if str(exc) not in ('batch_deadline','run_request_bound','intake_stopped'):
                state.stop(str(exc),time.time())
        finally:
            report['completed_at'] = time.time(); report['budget_after'] = budget.summary()
            report['pending'] = {stage:queue.db.execute('SELECT count(*) FROM candidates WHERE stage=?',(stage,)).fetchone()[0]
                                 for stage in ('discovered','detail','range','qualified')}
            state.record('run_end',{k:v for k,v in report.items() if k not in ('details','receipts','tickets')},time.time())
            with (WORKSPACE/'continuous-intake-20261021.jsonl').open('a',encoding='utf-8') as out:
                out.write(json.dumps(report,default=str,ensure_ascii=False)+'\n')
            queue.close(); obs.close(); budget.close()
        return report
    finally:
        guard.close()
