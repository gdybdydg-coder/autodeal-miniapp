"""Durable, user-started scans of every search-result page, separate from delivery.

Each short step shares RiaSearch's cache, request caps and provider lease. Candidate
IDs and completed results are committed in PostgreSQL, not in expiring cursors.
Only authenticated POST/PATCH requests start/resume work; reading progress is free.
"""
import asyncio
import copy
import logging
import time
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .auto_ria import RiaError
from .models import Filters, FullScan, ScanItem
from .ria_search import FRESH_SECONDS, PAGE_REQUEST_LIMIT, RiaSearch, estimate, matches, parse_ids, quota_status

log = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "waiting")
PAGE_SIZE = 50
LEASE_SECONDS = 90


def start(db, uid, filters, restart=False):
    """Caller holds the user's row lock, serializing starts across browser tabs."""
    filters = filters.model_copy(update={"onlyDeals": False})
    fingerprint = filters.fingerprint()
    row = db.scalar(select(FullScan).where(FullScan.user_id == uid, FullScan.fingerprint == fingerprint))
    if row is None and db.scalar(select(func.count()).select_from(FullScan).where(FullScan.user_id == uid)) >= 20:
        # These are derived results, not saved subscriptions. Evict only the oldest
        # inactive scan; never delete a running job or touch the user's searches.
        old = db.scalar(select(FullScan).where(FullScan.user_id == uid, FullScan.status.not_in(ACTIVE))
                        .order_by(FullScan.updated_at).limit(1))
        if old:
            db.execute(delete(ScanItem).where(ScanItem.scan_id == old.id))
            db.delete(old)
    db.execute(update(FullScan).where(FullScan.user_id == uid, FullScan.fingerprint != fingerprint,
                                      FullScan.status.in_(ACTIVE)).values(status="paused"))
    now = time.time()
    if row is None:
        row = FullScan(id=uuid.uuid4().hex, user_id=uid, fingerprint=fingerprint,
                       generation=uuid.uuid4().hex, filters=filters.canonical(), created_at=now, updated_at=now)
        db.add(row)
    elif restart or (row.status == "completed" and now - row.updated_at >= FRESH_SECONDS):
        db.execute(delete(ScanItem).where(ScanItem.scan_id == row.id))
        row.generation, row.context = uuid.uuid4().hex, {}
        row.source_total = row.discovered = row.checked = row.unavailable = row.requests = 0
        row.owner, row.lease_until = "", 0
        row.created_at = now
        row.status = "queued"
    if row.status not in ("completed", "running", "waiting"):
        row.status, row.error, row.next_run = "queued", "", 0
    if row.status != "completed":
        row.updated_at = now
    db.flush()
    return row.id


def change(db, uid, scan_id, enabled):
    row = db.scalar(select(FullScan).where(FullScan.id == scan_id, FullScan.user_id == uid).with_for_update())
    if not row:
        return False
    if not enabled:
        if row.status in ACTIVE:
            row.status = "paused"
    elif row.status not in ("completed", "running", "waiting"):
        db.execute(update(FullScan).where(FullScan.user_id == uid, FullScan.id != scan_id,
                                          FullScan.status.in_(ACTIVE)).values(status="paused"))
        row.status, row.error, row.next_run = "queued", "", 0
    return True


def view(db, uid, scan_id, after=0, only_deals=False):
    row = db.scalar(select(FullScan).where(FullScan.id == scan_id, FullScan.user_id == uid))
    if not row:
        return None
    now = time.time()
    criteria = [ScanItem.scan_id == scan_id, ScanItem.state == "checked"]
    if only_deals:
        criteria.append(ScanItem.deal.is_(True))
    records = list(db.scalars(select(ScanItem).where(*criteria, ScanItem.id > after)
                             .order_by(ScanItem.id).limit(101)))
    cars = []
    for item in records[:100]:
        car = copy.deepcopy(item.car)
        stale = now - item.checked_at >= FRESH_SECONDS
        car.update(checked_at=item.checked_at, stale=stale, historical_match=bool(stale and item.deal))
        if stale:
            car.update(market=None, discount=None, comparables=0, valuation="stale")
        cars.append(car)
    matching = db.scalar(select(func.count()).select_from(ScanItem).where(*criteria))
    deals = db.scalar(select(func.count()).select_from(ScanItem).where(
        ScanItem.scan_id == scan_id, ScanItem.deal.is_(True)))
    valued = db.scalar(select(func.count()).select_from(ScanItem).where(
        ScanItem.scan_id == scan_id, ScanItem.valued.is_(True)))
    return {"scan_id": row.id, "generation": row.generation, "status": row.status, "error": row.error,
            "phase": "checking" if row.context.get("enumerated") else "discovering",
            "cars": cars, "inspected": row.checked, "discovered": row.discovered,
            "source_total": row.source_total, "matching": matching, "deals_found": deals, "valued": valued,
            "unavailable": row.unavailable, "requests_used": row.requests,
            "created_at": row.created_at, "checked_at": row.updated_at,
            "retry_after_seconds": max(0, int(row.next_run - now)),
            "after": records[min(len(records), 100) - 1].id if records else after,
            "more_results": len(records) > 100, "complete": row.status == "completed",
            "warnings": [row.error] if row.error else [], "stale": False,
            "source": "AUTO.RIA", "source_url": "https://auto.ria.com/"}


class Scanner:
    def __init__(self, engine, key, search_factory=RiaSearch):
        self.engine, self.key, self.search_factory = engine, key, search_factory
        self.owner = uuid.uuid4().hex

    def claim(self):
        now = time.time()
        with Session(self.engine) as db:
            row = db.scalar(select(FullScan).where(FullScan.status.in_(ACTIVE), FullScan.next_run <= now,
                                                    FullScan.lease_until <= now)
                            .order_by(FullScan.next_run, FullScan.updated_at).limit(1))
            if row is None:
                return None
            scan_id = row.id
            result = db.execute(update(FullScan).where(FullScan.id == scan_id, FullScan.status.in_(ACTIVE),
                                                        FullScan.lease_until <= now).values(
                owner=self.owner, lease_until=now + LEASE_SECONDS, status="running"))
            db.commit()
            return scan_id if result.rowcount else None

    def current(self, db, scan_id):
        return db.scalar(select(FullScan).where(FullScan.id == scan_id, FullScan.owner == self.owner,
                                                 FullScan.status.in_(ACTIVE), FullScan.lease_until > time.time())
                         .with_for_update())

    def enumerate_page(self, scan_id, source, context):
        params = {**context["params"], "page": context["page"], "countpage": PAGE_SIZE}
        result = source.request("search", params, parse_ids, force=True)
        source_ids = result["ids"]
        with Session(self.engine) as db:
            row = self.current(db, scan_id)
            if row is None:
                return False
            existing = set(db.scalars(select(ScanItem.source_id).where(
                ScanItem.scan_id == scan_id, ScanItem.source_id.in_(source_ids))))
            new = [i for i in source_ids if i not in existing]
            for source_id in new:
                db.add(ScanItem(scan_id=scan_id, source_id=source_id))
            row.discovered += len(new)
            row.source_total = max(row.source_total, result["total"])
            expected = min(PAGE_SIZE, max(0, result["total"] - context["page"] * PAGE_SIZE))
            if len(source_ids) < expected or (source_ids and not new):
                # A repeated/truncated API page is not full coverage. Do not skip
                # to later pages or endlessly pay for the same response.
                context = {**context, "enumerated": True, "coverage_error": "pagination_incomplete"}
            else:
                context = {**context, "page": context["page"] + 1,
                           "enumerated": context["page"] * PAGE_SIZE + len(source_ids) >= result["total"]}
            row.context, row.updated_at = context, time.time()
            db.commit()
        return True

    def work(self, scan_id, source, stop):
        with Session(self.engine) as db:
            row = self.current(db, scan_id)
            if row is None:
                return
            filters = Filters.model_validate(row.filters)
            context = copy.deepcopy(row.context)
        if not context:
            params, ids = source.parameters(filters)
            context = {"params": params, "ids": ids, "page": 0, "enumerated": False}
            with Session(self.engine) as db:
                row = self.current(db, scan_id)
                if row is None:
                    return
                row.context = context
                db.commit()
        for _ in range(50):
            if stop and stop.is_set():
                return
            with Session(self.engine) as db:
                row = self.current(db, scan_id)
                if row is None:
                    return
                context = copy.deepcopy(row.context)
                # Capture every ID page before the slower peer valuation pass.
                # Evaluating a page for hours before requesting the next offset
                # would magnify skips caused by new ads moving the source pages.
                item = (db.scalar(select(ScanItem).where(ScanItem.scan_id == scan_id, ScanItem.state == "pending")
                                  .order_by(ScanItem.id).limit(1)) if context["enumerated"] else None)
                if item is None and context["enumerated"]:
                    row.status = "completed" if not context.get("coverage_error") and row.checked == row.discovered and row.discovered >= row.source_total else "incomplete"
                    row.error = "" if row.status == "completed" else context.get("coverage_error", "catalog_changed")
                    row.updated_at = time.time()
                    db.commit()
                    return
                source_id = item.source_id if item else None
            if source_id is None:
                if not self.enumerate_page(scan_id, source, context):
                    return
                continue
            car, state = None, "excluded"
            try:
                candidate = source.car(source_id)
                if matches(candidate, filters, context["ids"]):
                    rating = estimate(candidate, source.comparisons(candidate))
                    car, state = {**candidate, **rating}, "checked"
            except RiaError as exc:
                if str(exc) not in ("listing_unavailable", "invalid_response"):
                    raise
                state = "unavailable"
            with Session(self.engine) as db:
                row = self.current(db, scan_id)
                if row is None:
                    return
                item = db.scalar(select(ScanItem).where(ScanItem.scan_id == scan_id, ScanItem.source_id == source_id))
                item.car, item.state = car, state
                item.checked_at = min(time.time(), source.observed_at)
                item.deal = bool(car and car["valuation"] == "sample_median" and car["price_usd"] <= car["market"] * .85)
                item.valued = bool(car and car["valuation"] == "sample_median")
                row.checked += 1
                row.unavailable += int(state == "unavailable")
                row.error, row.updated_at = "", time.time()
                db.commit()

    def tick(self, stop=None):
        if not self.key or (stop and stop.is_set()):
            return
        scan_id = self.claim()
        if not scan_id:
            return
        source = self.search_factory(self.engine, self.key)
        acquired, reason = False, ""
        try:
            source.acquire()
            acquired = True
            source.request_limit = PAGE_REQUEST_LIMIT
            self.work(scan_id, source, stop)
        except RiaError as exc:
            reason = str(exc)
        except Exception as exc:
            # Never log provider URLs, credentials or exception messages.
            log.error("Full scan step failed (%s)", type(exc).__name__)
            reason = "source_unavailable"
        finally:
            if acquired:
                source.release()
            quota = quota_status(self.engine, source.limits)
            with Session(self.engine) as db:
                row = db.scalar(select(FullScan).where(FullScan.id == scan_id, FullScan.owner == self.owner).with_for_update())
                if row:
                    row.requests += source.requests_made
                    row.lease_until = 0
                    if row.status in ACTIVE:
                        row.next_run = time.time() + 2
                        row.status, row.error = "queued", ""
                        if reason == "quota_exceeded":
                            row.status = "budget_exhausted" if quota["reason"] == "total" else "waiting"
                            row.error = reason
                            row.next_run = time.time() + max(5, quota["retry_after_seconds"] or 3600)
                        elif reason in ("busy", "search_limit"):
                            row.next_run = time.time() + (5 if reason == "busy" else 2)
                        elif reason in ("connection_error", "upstream_error", "source_unavailable"):
                            row.status, row.error, row.next_run = "waiting", reason, time.time() + 60
                        elif reason:
                            row.status, row.error = "error", reason
                    db.commit()


async def run(engine, key, stop):
    scanner = Scanner(engine, key)
    while not stop.is_set():
        try:
            await asyncio.to_thread(scanner.tick, stop)
        except Exception as exc:
            log.error("Full scanner unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass
