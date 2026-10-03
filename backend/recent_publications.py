"""Shared, opt-in intake of dated public publications, never an active catalog.

The collector only writes a bounded SourceProbe queue. A bounded monitor step validates
each candidate with official details before sharing the existing AI/delivery path.
Publication evidence requires matching HTML and API addition dates. Update dates,
first-seen IDs and preview prices never authorize a notification.
"""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
import math
import re
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import html_shadow
from .auto_ria import RiaError
from .models import Filters, MonitorJob, MonitorSeen, MonitorWatch, Search, SourceBudget, SourceProbe
from .ria_budget import BudgetLimits
from .ria_search import RiaSearch, budget_state, matches, normalize
from .valuation import notification_condition_allowed

PROBE_ID = "recent-publications-v1"
KIND = "html_new_publication"
RETIRED_STATE = "html_cancelled"
RETIREMENT_REASONS = {"recent_publications_disabled", "html_publication_expired"}
KYIV = ZoneInfo("Europe/Kyiv")
MAX_AGE, MAX_QUEUE, MAX_SEEN = 3600, 128, 10000
HTTP_HOURLY, HTTP_DAILY, HTTP_BYTES = 125, 1600, 3 * 1024**3
API_HOURLY, API_DAILY = 200, 1000
DENIED = {"access_denied", "robots_denied"}


def enabled(settings):
    return bool(getattr(settings, "ria_recent_publications_enabled", False)
                and settings.live and settings.monitor_enabled and settings.ria_ai_price_enabled)


def interval(now=None):
    hour = datetime.fromtimestamp(time.time() if now is None else now, KYIV).hour
    return 3600 if hour >= 23 or hour < 8 else 110 if hour < 18 else 60


def added_at(value):
    """An explicit source-clock policy; reject ambiguous/nonexistent DST times."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
                return None
            early, late = stamp.replace(tzinfo=KYIV, fold=0), stamp.replace(tzinfo=KYIV, fold=1)
            if early.utcoffset() != late.utcoffset():
                return None
            if datetime.fromtimestamp(early.timestamp(), KYIV).replace(tzinfo=None) != stamp:
                return None
            stamp = early
        result = stamp.timestamp()
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, OverflowError):
        return None


class Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.current, self.depth, self.count = [], None, 0, 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        classes = set(data.get("class", "").split())
        if tag == "section":
            if "ticket-item" in classes:
                if self.current is not None:
                    raise html_shadow.ProbeError("unexpected_html")
                self.count += 1
                self.current = {"id": data.get("data-advertisement-id"), "added": [], "prices": [],
                                "links": [], "promoted": False, "previews": []}
                self.depth = 1
            elif self.current is not None:
                self.depth += 1
        if self.current is None:
            return
        if classes & {"paid", "sponsored", "native-ad", "ticket-item--advert", "ticket-item--paid"}:
            self.current["promoted"] = True
        if any(data.get(k) in {"true", "1"} for k in ("data-sponsored", "data-is-advert", "data-is-paid")):
            self.current["promoted"] = True
        if "data-add-date" in data:
            self.current["added"].append(added_at(data["data-add-date"]))
        if "data-advertisement-data" in data and data.get("data-id") == self.current["id"]:
            preview = {}
            for name, attribute in (("brand", "data-mark-name"), ("model", "data-model-name")):
                value = data.get(attribute)
                if isinstance(value, str) and 0 < len(value.strip()) <= 150:
                    preview[name] = value.strip()
            year = data.get("data-year", "")
            if re.fullmatch(r"(?:19|20)[0-9]{2}", year):
                preview["year"] = int(year)
            self.current["previews"].append(preview)
        if "price-ticket" in classes and data.get("data-main-currency") == "USD":
            value = data.get("data-main-price", "")
            if re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
                self.current["prices"].append(float(value))
        if tag == "a" and "m-link-ticket" in classes:
            self.current["links"].append(data.get("href", ""))

    def handle_endtag(self, tag):
        if tag == "section" and self.current is not None:
            self.depth -= 1
            if self.depth == 0:
                self.rows.append(self.current)
                self.current = None


def parse(body):
    parser = Cards()
    parser.feed(body)
    if parser.current is not None or not 1 <= parser.count <= 200 or len(parser.rows) != parser.count:
        raise html_shadow.ProbeError("unexpected_html")
    result, identifiers = [], set()
    for row in parser.rows:
        sid = row["id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", sid) or sid in identifiers:
            raise html_shadow.ProbeError("unexpected_html")
        identifiers.add(sid)
        if row["promoted"] or len(row["added"]) != 1 or row["added"][0] is None:
            continue
        if len(row["links"]) != 1:
            continue
        url = urlsplit(row["links"][0])
        if (url.scheme != "https" or url.netloc != "auto.ria.com" or url.query or url.fragment
                or not re.fullmatch(r"/uk/auto_[A-Za-z0-9_-]+_" + sid + r"\.html", url.path)):
            continue
        prices = row["prices"]
        # An absent non-USD preview is not a price claim; the API remains authoritative.
        price = prices[0] if len(prices) == 1 else None
        if price is not None and price <= 0:
            continue
        item = {"id": sid, "added_at": row["added"][0], "preview_usd": price}
        # A conflicting/missing preview is unknown, never a reason to lose a car.
        previews = row["previews"]
        if previews and all(value == previews[0] for value in previews) and previews[0]:
            item["preview"] = previews[0]
        result.append(item)
    return result


def initial():
    return {"baseline_at": None, "pending": {}, "seen": {}, "http": [], "api": [],
            "cycles": 0, "queued": 0, "validated": 0, "duplicates": 0, "discarded": 0,
            "queue_overflow": 0, "discard_reasons": {}, "errors": 0, "next_at": 0, "robots_until": 0}


def discard(data, reason, count=1):
    data["discarded"] += count
    reasons = data.setdefault("discard_reasons", {})
    reasons[reason] = reasons.get(reason, 0) + count


def disable(db):
    row = db.get(SourceProbe, PROBE_ID)
    if row and row.status != "disabled":
        data = copy.deepcopy(row.result)
        data.update(baseline_at=None, pending={}, next_at=0)
        row.result, row.status = data, "disabled"
        # Accounting, deny/backoff evidence and all monitor/delivery claims remain.


def collect(engine, settings, fetcher=html_shadow.fetch, now=None):
    if not enabled(settings):
        return
    now, owner = time.time() if now is None else now, uuid.uuid4().hex
    with Session(engine) as db:
        if db.get(SourceProbe, PROBE_ID) is None:
            db.add(SourceProbe(id=PROBE_ID, status="starting", checked_at=0, requests=0, result=initial()))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update(skip_locked=True))
        if row is None:
            return
        data = copy.deepcopy(row.result)
        if data.get("blocked") in DENIED or now < max(data.get("next_at", 0), data.get("lease_until", 0)):
            return
        from .monitor import active_members
        if not active_members(db) or not html_shadow.healthy(db, now):
            row.status, data["next_at"] = "yielding_to_bot", now + interval(now)
            row.result = data
            db.commit()
            return
        data["http"] = [event for event in data["http"] if event[0] > now - 86400]
        robots_due = now >= data.get("robots_until", 0)
        reserve = len(html_shadow.PAGES) + int(robots_due)
        byte_reserve = len(html_shadow.PAGES) * html_shadow.MAX_BODY + int(robots_due) * 100000
        if (sum(e[1] for e in data["http"] if e[0] > now - 3600) + reserve > HTTP_HOURLY
                or sum(e[1] for e in data["http"]) + reserve > HTTP_DAILY
                or sum(e[2] for e in data["http"]) + byte_reserve > HTTP_BYTES):
            row.status, data["next_at"] = "http_budget_wait", now + 300
            row.result = data
            db.commit()
            return
        # Reserve durably before I/O. A crash consumes the full reservation.
        data["http"].append([now, reserve, byte_reserve, owner])
        data.update(lease_owner=owner, lease_until=now + 90, next_at=now + interval(now))
        row.requests += reserve
        row.status, row.result = "collecting", data
        db.commit()
    rows, conflicts, transferred, error = {}, set(), 0, None
    try:
        if robots_due:
            body, size = fetcher(html_shadow.ROBOTS)
            transferred += size
            if not all(html_shadow.robots_allow(body, url) for url in html_shadow.PAGES):
                raise html_shadow.ProbeError("robots_denied")
        for url in html_shadow.PAGES:
            body, size = fetcher(url)
            transferred += size
            for item in parse(body):
                previous = rows.get(item["id"])
                if item["id"] in conflicts:
                    continue
                if previous is not None and previous["added_at"] != item["added_at"]:
                    conflicts.add(item["id"])
                    rows.pop(item["id"])
                    continue
                if previous is not None and previous["preview_usd"] != item["preview_usd"]:
                    item["preview_usd"] = None
                if previous is not None and previous.get("preview") != item.get("preview"):
                    item.pop("preview", None)
                rows[item["id"]] = item
        if not rows and not conflicts:
            raise html_shadow.ProbeError("unexpected_html")
    except html_shadow.ProbeError as exc:
        error = exc
    except Exception:
        error = html_shadow.ProbeError("transport_error")
    finished = time.time()
    with Session(engine) as db:
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update())
        data = copy.deepcopy(row.result)
        if data.get("lease_owner") != owner or row.status == "disabled":
            return
        data["lease_until"] = 0
        if error:
            data["errors"] += 1
            data["last_error"] = error.reason
            data["next_at"] = finished + max(interval(finished), error.retry)
            if error.reason in DENIED:
                data["blocked"] = error.reason
            row.status = error.reason if error.reason in DENIED else "backoff"
        else:
            for event in data["http"]:
                if event[3] == owner:
                    event[2] = transferred
            data["errors"], data["last_success_at"] = 0, finished
            data["last_error"] = None
            if robots_due:
                data["robots_until"] = finished + 21600
            # The whole first successful snapshot is a baseline, even if its
            # cards were published during the HTTP requests.
            if data["baseline_at"] is None:
                data["baseline_at"] = finished
                row.status = "baseline"
            else:
                data["seen"] = {sid: at for sid, at in data["seen"].items() if at > finished - 2 * MAX_AGE}
                before_expiry = len(data["pending"])
                data["pending"] = {sid: item for sid, item in data["pending"].items()
                                   if item["added_at"] > finished - MAX_AGE}
                if before_expiry > len(data["pending"]):
                    discard(data, "expired", before_expiry - len(data["pending"]))
                if len(data["seen"]) >= MAX_SEEN:
                    row.status = "storage_wait"
                else:
                    for sid, item in sorted(rows.items(), key=lambda pair: (-pair[1]["added_at"], pair[0])):
                        if (sid in data["seen"] or sid in data["pending"] or
                                not max(data["baseline_at"], finished - MAX_AGE) < item["added_at"] <= finished):
                            continue
                        if len(data["seen"]) >= MAX_SEEN:
                            break
                        data["seen"][sid] = item["added_at"]
                        if db.get(MonitorJob, sid) is not None:
                            data["duplicates"] += 1
                            continue
                        if len(data["pending"]) >= MAX_QUEUE:
                            data["queue_overflow"] += 1
                            continue
                        data["pending"][sid] = {**item, "attempts": 0, "next_at": 0}
                        data["queued"] += 1
                    row.status = "watching"
            data["cycles"] += 1
        row.result, row.checked_at = data, finished
        db.commit()


def api_gate(db, data, now, limits):
    """Read-only local availability; a provider balance is never inferred."""
    budget = db.get(SourceBudget, "auto_ria")
    if budget is None:
        return {"reason": "accounting_unavailable", "retry_at": None}
    shared = budget_state(budget, now, limits, db=db)
    if shared["reason"] == "total":
        return {"reason": "shared_total", "retry_at": None}
    waits = []
    if shared["reason"] != "available":
        waits.append((now + shared["retry_after_seconds"], "shared_" + shared["reason"]))
    for calls, seconds, cap, reason in (
        (budget.calls, 3600, limits.hourly * 4 // 5, "primary_hourly_headroom"),
        (budget.calls, 86400, limits.daily * 4 // 5, "primary_daily_headroom"),
        (data.get("api", []), 3600, API_HOURLY, "intake_hourly"),
        (data.get("api", []), 86400, API_DAILY, "intake_daily"),
    ):
        events = sorted(at for at in calls if at > now - seconds)
        if cap <= 0:
            return {"reason": reason, "retry_at": None}
        if len(events) >= cap:
            waits.append((events[-cap] + seconds, reason))
    if waits:
        at, reason = max(waits)
        return {"reason": reason, "retry_at": at}
    return {"reason": "available", "retry_at": now}


def reserve_api(db, now, limits):
    """Extra calls retain primary headroom and share the unchanged hard caps."""
    row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update())
    if row is None or row.status == "disabled":
        raise RiaError("reserved_for_new_publications")
    data = copy.deepcopy(row.result)
    if api_gate(db, data, now, limits)["reason"] != "available":
        raise RiaError("reserved_for_new_publications")
    data["api"] = [at for at in data["api"] if at > now - 86400]
    data["api"].append(now)
    row.result = data
    # Committed atomically with RiaSearch's normal budget reservation before fetch.


def preview_matches(item, filters):
    """Reject only explicit same-ID brand/model/year/price contradictions.

    Optional preview data never authorizes valuation/delivery and unknown values
    retain the existing official-detail path. No location or category is guessed.
    """
    preview = item.get("preview") or {}
    for field in ("brand", "model"):
        wanted, actual = getattr(filters, field), preview.get(field)
        if wanted and actual and normalize(wanted) != normalize(actual):
            return False
    for value, bounds in ((item.get("preview_usd"), filters.price),
                          (preview.get("year"), filters.year)):
        if value is not None and ((bounds.from_ is not None and value < bounds.from_)
                                  or (bounds.to is not None and value > bounds.to)):
            return False
    return True


def defer_candidate(monitor, sid, item, reason, attempted, limits):
    """Quota/access/lease waits do not exhaust a detail transport retry budget."""
    now = time.time()
    with monitor._state_lock, Session(monitor.engine) as db:
        if not monitor.owned(db):
            return
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update())
        data = copy.deepcopy(row.result)
        current = data["pending"].get(sid)
        if not current or current["added_at"] != item["added_at"]:
            return
        current["attempts"] = item["attempts"] + int(attempted)
        current["wait_reason"] = reason
        gate = api_gate(db, data, now, limits)
        current["next_at"] = max(now + 1, gate["retry_at"]) if gate["reason"] != "available" and gate["retry_at"] else now + 60
        row.result = data
        db.commit()


def valid_proof(evidence, now=None):
    now = time.time() if now is None else now
    added, baseline, expires = (evidence.get(k) for k in ("html_added_at", "html_baseline_at", "html_expires_at"))
    return (evidence.get("html_verified") is True and
            all(type(v) in (int, float) and math.isfinite(v) for v in (added, baseline, expires)) and
            0 < baseline < added <= now <= expires and expires == added + MAX_AGE and
            evidence.get("publication_after") == added)


def expired_proof(evidence, now=None):
    """Previously valid supplementary proof; expiration is not a sold-car fact."""
    now = time.time() if now is None else now
    if not isinstance(evidence, dict):
        return False
    expires = evidence.get("html_expires_at")
    return (type(expires) in (int, float) and expires < now
            and valid_proof(evidence, expires))


def retired_interest(db, seen, job, uid, now):
    """Only dated primary discovery may recover unclaimed HTML retirement.

    Old releases erased origin/reason on expiry but retained the verified proof.
    No delivered/uncertain/cancelled Telegram claim is ever reopened here.
    The caller separately checks current payment, /stop and watch epoch.
    """
    if seen is None or job is None:
        return False
    evidence = job.result if isinstance(job.result, dict) else {}
    promoted = (job.state in {"pending", "checked"}
                and evidence.get("discovery_kind") == "new_publication")
    if job.state != "cancelled" and not promoted:
        return False
    explicit = seen.state == RETIRED_STATE and (job.reason in RETIREMENT_REASONS or promoted)
    # First recipient promotion changes the shared job, while each other
    # recipient still owns its retired seen row. Retain the dated HTML shape
    # check but do not treat the new primary lower boundary as an HTML date.
    legacy_proof = ({**evidence, "publication_after": evidence.get("html_added_at")}
                    if promoted else evidence)
    legacy = (seen.state == "cancelled" and not job.reason
              and expired_proof(legacy_proof, now))
    if not (explicit or legacy):
        return False
    from .shared_distribution import claimed
    return not claimed(db, uid, job.source_id)


def due(engine):
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        return bool(row and row.result.get("baseline_at") and row.result.get("blocked") not in DENIED
                    and any(item["next_at"] <= time.time() for item in row.result.get("pending", {}).values()))


def take(monitor, source, *, after_primary=False):
    """One shared detail validation, with a durable saturated-loop throttle."""
    if not enabled(monitor.settings):
        return False
    from .monitor import active_members, source_filters, bind_source_access, member_query
    from . import paid_source_access
    from .shared_distribution import claimed
    now = time.time()
    with monitor._state_lock, Session(monitor.engine) as db:
        if not monitor.owned(db):
            return False
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update())
        if row is None or row.result.get("baseline_at") is None or row.result.get("blocked") in DENIED:
            return False
        data = copy.deepcopy(row.result)
        if after_primary and data.get("after_primary_next_at", 0) > now:
            return False
        choices = [(sid, item) for sid, item in data["pending"].items() if item["next_at"] <= now]
        if not choices:
            return False
        sid, item = min(choices, key=lambda pair: (pair[1]["added_at"], pair[0]))
        if after_primary:
            data["after_primary_next_at"] = now + 15
        baseline = data["baseline_at"]
        targets = []
        if item["added_at"] > now - MAX_AGE and db.get(MonitorJob, sid) is None:
            for search, watch, member in active_members(db):
                if member.started_at > item["added_at"] or db.get(MonitorSeen, (search.id, sid)) is not None or claimed(db, search.user_id, sid):
                    continue
                filters = Filters.model_validate(search.filters)
                if not preview_matches(item, filters):
                    continue
                try:
                    _, resolved = RiaSearch.cached_parameters(db, source_filters(filters))
                except RiaError:
                    continue
                targets.append((search.id, search.user_id, watch.epoch, resolved))
        if not targets:
            data["pending"].pop(sid)
            discard(data, "expired" if item["added_at"] <= now - MAX_AGE else "no_eligible_subscription")
            row.result = data
            db.commit()
            return True
        gate = api_gate(db, data, now, source.limits)
        if gate["reason"] != "available":
            item["wait_reason"] = gate["reason"]
            item["next_at"] = max(now + 1, gate["retry_at"]) if gate["retry_at"] else now + 300
            data["pending"][sid] = item
            row.result = data
            db.commit()
            return True
        item["next_at"] = now + 60
        data["pending"][sid] = item
        row.result = data
        db.commit()
    source.request_policy = reserve_api
    target_epochs = [(search_id, uid, epoch) for search_id, uid, epoch, _ in targets]
    bind_source_access(source, lambda now: member_query(now, **paid_source_access.query_options(source.engine)).where(
        tuple_(Search.id, Search.user_id, MonitorWatch.epoch).in_(target_epochs)),
        reason="recent_publication_validation", group="auto_ria:" + sid)
    requests_before = source.requests_made
    try:
        candidate = source.car(sid, force=True)
    except RiaError as exc:
        attempted = source.requests_made > requests_before
        if str(exc) in {"quota_exceeded", "reserved_for_new_publications", "busy", "search_limit",
                        "no_eligible_subscription", "paid_access_required"}:
            defer_candidate(monitor, sid, item, str(exc), attempted=False, limits=source.limits)
            return True
        if str(exc) in {"connection_error", "upstream_error"} and item["attempts"] + int(attempted) < 3:
            defer_candidate(monitor, sid, item, str(exc), attempted=attempted, limits=source.limits)
            return True
        candidate = None
    with monitor._state_lock, Session(monitor.engine) as db:
        if not monitor.owned(db):
            return True
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == PROBE_ID).with_for_update())
        data = copy.deepcopy(row.result)
        if data["pending"].get(sid, {}).get("added_at") != item["added_at"]:
            return True
        data["pending"].pop(sid)
        proof = {"discovery_kind": KIND, "publication_after": item["added_at"],
                 "html_added_at": item["added_at"], "html_baseline_at": baseline,
                 "html_expires_at": item["added_at"] + MAX_AGE, "html_verified": True}
        valid = (candidate and candidate.get("category_id") == 1 and valid_proof(proof)
                 and added_at(candidate.get("source_add_date_text")) == item["added_at"]
                 and notification_condition_allowed(candidate) and db.get(MonitorJob, sid) is None)
        recipients, filters_by_key = [], {}
        if valid:
            for search_id, uid, epoch, resolved in targets:
                current = monitor.current(db, search_id, uid, epoch)
                if not current or db.get(MonitorSeen, (search_id, sid)) is not None or claimed(db, uid, sid):
                    continue
                search, _ = current
                filters = Filters.model_validate(search.filters)
                if matches(candidate, filters, resolved):
                    recipients.append((search_id, epoch))
                    filters_by_key[source_filters(filters).fingerprint()] = resolved
        if recipients:
            db.add(MonitorJob(source_id=sid, first_seen=time.time(),
                result={**proof, "candidate": candidate, "filters": filters_by_key}))
            for search_id, epoch in recipients:
                db.add(MonitorSeen(search_id=search_id, source_id=sid, epoch=epoch,
                                   state="pending", first_seen=time.time()))
            data["validated"] += 1
        else:
            reason = ("unavailable_details" if not candidate else
                      "not_passenger_car" if candidate.get("category_id") != 1 else
                      "addition_date_mismatch" if added_at(candidate.get("source_add_date_text")) != item["added_at"] else
                      "foreign_or_custom" if not notification_condition_allowed(candidate) else
                      "filter_or_activation_mismatch")
            discard(data, reason)
        row.result = data
        db.commit()
    return True


def status(engine, settings):
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        if row is None:
            return {"enabled": enabled(settings), "status": "starting" if enabled(settings) else "disabled"}
        data, now = row.result, time.time()
        return {"enabled": enabled(settings), "status": row.status, "scope": "two_dated_public_pages",
                "source_timezone": "Europe/Kyiv", "baseline_at": data.get("baseline_at"),
                "interval_seconds": interval(now), "pending_candidates": len(data.get("pending", {})),
                "reserved_http_requests": row.requests, "http_calls_last_hour": sum(
                    e[1] for e in data.get("http", []) if e[0] > now - 3600),
                "paid_calls_last_hour": sum(at > now - 3600 for at in data.get("api", [])),
                "paid_calls_last_day": sum(at > now - 86400 for at in data.get("api", [])),
                "limits": {"http_hourly": HTTP_HOURLY, "http_daily": HTTP_DAILY,
                           "http_bytes_daily": HTTP_BYTES, "paid_hourly": API_HOURLY,
                           "paid_daily": API_DAILY, "maximum_pending_candidates": MAX_QUEUE},
                **{k: data.get(k) for k in ("cycles", "queued", "validated", "duplicates", "discarded", "discard_reasons",
                    "queue_overflow", "last_success_at", "last_error")},
                "api_availability": api_gate(db, data, now, BudgetLimits.env()),
                "waiting_candidates": sum(bool(item.get("wait_reason")) for item in data.get("pending", {}).values()),
                "whole_market_coverage_measured": False}


async def run(settings, stop):
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recent-publications")
    try:
        while not stop.is_set():
            await asyncio.get_running_loop().run_in_executor(pool, collect, engine, settings)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
    finally:
        pool.shutdown(wait=True)
        engine.dispose()
