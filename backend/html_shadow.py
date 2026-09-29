"""Bounded public-HTML observation only. No provider credentials or deliveries.

Writes one namespaced SourceProbe; production tables are read-only. A sample of
two pages is NOT a replacement search or a measurement of whole-market recall.
"""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Delivery, MonitorControl, MonitorJob, SourceProbe

UA = "AUTODeal-Research/0.1"
ROBOTS = "https://auto.ria.com/robots.txt"
PAGES = ("https://auto.ria.com/uk/last/hour/", "https://auto.ria.com/uk/last/hour/?page=2")
INTERVAL, DURATION = 300, 86400
MAX_IDS, MAX_REQUESTS, MAX_BODY, MAX_BYTES = 10000, 600, 2500000, 1024**3
TERMINAL = {"completed", "request_cap", "storage_cap", "byte_cap", "access_denied", "robots_denied", "failed"}
log = logging.getLogger(__name__)


class ProbeError(Exception):
    def __init__(self, reason, retry=900):
        self.reason, self.retry = reason, retry


def key(settings):
    run_id = getattr(settings, "ria_html_shadow_run_id", "")
    return "html-shadow-v1-" + run_id if re.fullmatch(r"[a-z0-9-]{1,32}", run_id) else None


class Cards(HTMLParser):
    """Only card IDs are retained. No descriptions, phones, VINs or user IDs."""
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.cards = 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "section" and "ticket-item" in data.get("class", "").split():
            self.cards += 1
            ident = data.get("data-advertisement-id", "")
            if re.fullmatch(r"[1-9][0-9]{0,11}", ident):
                self.ids.add(ident)


def parse_cards(body):
    parser = Cards()
    parser.feed(body)
    if not parser.ids or len(parser.ids) > 200 or parser.cards != len(parser.ids):
        raise ProbeError("unexpected_html")
    return parser.ids


def robots_allow(body, url):
    """User-agent groups, wildcard paths and longest-match allow/disallow."""
    groups, agents, rules, directives = [], [], [], False
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if directives:
                groups.append((agents, rules))
                agents, rules, directives = [], [], False
            agents.append(value.lower())
        elif name in {"allow", "disallow"} and agents:
            directives = True
            if value:
                rules.append((name, value))
    groups.append((agents, rules))
    if not any(a for a, _ in groups):
        raise ProbeError("invalid_robots")
    selected, specificity = [], -1
    for agents, rules in groups:
        rank = max((0 if a == "*" else len(a) for a in agents
                    if a == "*" or a in UA.lower()), default=-1)
        if rank > specificity:
            selected, specificity = list(rules), rank
        elif rank == specificity and rank >= 0:
            selected.extend(rules)
    parts = urlsplit(url)
    path = parts.path + ("?" + parts.query if parts.query else "")
    matches = []
    for name, rule in selected:
        end = rule.endswith("$")
        pattern = rule[:-1] if end else rule
        regex = "^" + ".*".join(re.escape(s) for s in pattern.split("*")) + ("$" if end else "")
        if re.search(regex, path):
            matches.append((len(pattern.replace("*", "")), name == "allow"))
    return max(matches, default=(0, True))[1]


def fetch(url):
    if url not in (ROBOTS, *PAGES):
        raise ProbeError("access_denied")
    started = time.monotonic()
    # No authenticated sessions, cookies, hidden endpoints or redirect following.
    with httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False,
                      headers={"User-Agent": UA}) as client:
        with client.stream("GET", url) as response:
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise ProbeError("access_denied")
            if response.status_code == 429:
                value = response.headers.get("Retry-After", "900")
                try:
                    retry = int(value) if value.isdecimal() else parsedate_to_datetime(value).timestamp()-time.time()
                except (ValueError, TypeError, OverflowError):
                    retry = DURATION
                retry = max(900, retry)
                raise ProbeError("rate_limited", retry)
            if response.status_code != 200:
                raise ProbeError("http_error")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > (100000 if url == ROBOTS else MAX_BODY):
                    raise ProbeError("oversize")
                if time.monotonic() - started > 25:
                    raise ProbeError("deadline")
                chunks.append(chunk)
            body = b"".join(chunks).decode("utf-8", "replace")
    return body, size


def healthy(db, now):
    row = db.get(MonitorControl, "pilot")
    if not row or now - row.heartbeat > 180:
        return False
    # Yield before a large backlog; these queries use the existing state indexes.
    for model in (MonitorJob, Delivery):
        if len(list(db.scalars(select(model.state).where(model.state == "pending").limit(100)))) >= 100:
            return False
    try:
        with open("/proc/self/statm") as f:
            import os
            if int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") > 384 * 1024**2:
                return False
    except (OSError, ValueError, IndexError):
        pass
    return True


def tick(engine, settings, fetcher=fetch, now=None):
    ident, now = key(settings), time.time() if now is None else now
    if not ident:
        return
    with Session(engine) as db:
        if db.get(SourceProbe, ident) is None:
            db.add(SourceProbe(id=ident, status="starting", checked_at=0, requests=0,
                result={"started_at": now, "ends_at": now+DURATION, "next_at": now,
                        "html": {}, "api": {}, "baseline": [], "cycles": 0, "bytes": 0, "errors": 0}))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
        row = db.scalar(select(SourceProbe).where(SourceProbe.id == ident).with_for_update(skip_locked=True))
        if row is None or row.status in TERMINAL:
            return
        data = copy.deepcopy(row.result)
        if now >= data["ends_at"]:
            row.status = "completed"
            db.commit()
            return
        if now < data.get("next_at", 0):
            return
        if not healthy(db, now):
            row.status = "yielding_to_bot"
            data["next_at"] = now+INTERVAL
            row.result = data
            db.commit()
            return
        robots_due = now >= data.get("robots_until", 0)
        reserve = len(PAGES) + int(robots_due)
        if row.requests + reserve > MAX_REQUESTS:
            row.status = "request_cap"
            db.commit()
            return
        if data["bytes"] + reserve * MAX_BODY > MAX_BYTES:
            row.status = "byte_cap"
            db.commit()
            return
        # Reserve before I/O. A crash may overcount, never replenish the cap.
        row.requests += reserve
        data["next_at"] = now+INTERVAL
        row.status, row.result = "running", data
        db.commit()
    ids, transferred = set(), 0
    error = None
    try:
        if robots_due:
            body, size = fetcher(ROBOTS)
            transferred += size
            if not all(robots_allow(body, url) for url in PAGES):
                raise ProbeError("robots_denied")
            data["robots_until"] = now+21600
        for url in PAGES:
            if time.time() >= data["ends_at"]:
                raise ProbeError("completed")
            body, size = fetcher(url)
            transferred += size
            ids.update(parse_cards(body))
        if not ids:
            raise ProbeError("unexpected_html")
    except ProbeError as exc:
        error = exc
    except Exception:
        error = ProbeError("transport_error")
    # Network waits never hold a DB connection or any production lock.
    with Session(engine) as db:
        row = db.get(SourceProbe, ident)
        data["bytes"] += transferred
        if error:
            data["errors"] += 1
            data["last_error"] = error.reason
            data["next_at"] = time.time()+error.retry
            row.status = (error.reason if error.reason in TERMINAL else
                          "failed" if data["errors"] >= 3 else "backoff")
        else:
            data["errors"] = 0
            if not data["cycles"]:
                data["baseline"] = sorted(ids)
            fresh_ids = ids - data["html"].keys()
            if len(data["html"]) + len(fresh_ids) > MAX_IDS:
                row.status = "storage_cap"
            else:
                found_at = time.time()
                for source_id in fresh_ids:
                    data["html"][source_id] = found_at
                # Compare only sampled IDs with existing jobs: no new API calls.
                missing = sorted(data["html"].keys() - data["api"].keys())
                for offset in range(0, len(missing), 500):
                    query = select(MonitorJob.source_id, MonitorJob.first_seen).where(
                        MonitorJob.source_id.in_(missing[offset:offset+500]))
                    for source_id, first_seen in db.execute(query):
                        data["api"][source_id] = first_seen
                data["cycles"] += 1
                data["last_page_ids"] = len(ids)
                data["last_success_at"] = found_at
                row.status = "watching"
        if data["bytes"] >= MAX_BYTES:
            row.status = "byte_cap"
        if time.time() >= data["ends_at"]:
            row.status = "completed"
        row.result, row.checked_at = data, time.time()
        db.commit()


def status(engine, settings):
    ident = key(settings)
    if not ident:
        return {"enabled": False}
    with Session(engine) as db:
        row = db.get(SourceProbe, ident)
        if not row:
            return {"enabled": True, "status": "starting"}
        d = row.result
        baseline = set(d.get("baseline", []))
        comparable = [d["html"][sid]-at for sid, at in d.get("api", {}).items()
                      if sid not in baseline and at >= d["started_at"]]
        return {"enabled": True, "status": row.status, "scope": "two_public_pages_sample",
            "started_at": d["started_at"], "ends_at": d["ends_at"], "next_at": d.get("next_at"),
            "interval_seconds": INTERVAL, "successful_cycles": d["cycles"],
            "reserved_http_requests": row.requests, "downloaded_bytes": d["bytes"],
            "unique_html_ids": len(d["html"]), "baseline_ids": len(baseline),
            "also_in_existing_api_jobs": len(d["api"]), "post_baseline_overlaps": len(comparable),
            "comparison_basis": "first_observed_id_not_publication_time",
            "html_observed_earlier": sum(v < 0 for v in comparable),
            "api_observed_earlier": sum(v > 0 for v in comparable),
            "last_success_at": d.get("last_success_at"), "last_error": d.get("last_error"),
            "consecutive_errors": d["errors"], "whole_market_coverage_measured": False,
            "creates_notifications": False, "paid_api_calls": 0}


async def run(settings, stop):
    # Separate one-connection pool and one thread; never consume monitor workers.
    engine, pool = None, ThreadPoolExecutor(max_workers=1, thread_name_prefix="html-shadow")
    try:
        kwargs = {"pool_pre_ping": True, "pool_size": 1, "max_overflow": 0, "pool_timeout": 2}
        if settings.database_url.startswith("postgresql"):
            kwargs["connect_args"] = {"connect_timeout": 5,
                "options": "-c statement_timeout=2000 -c lock_timeout=500"}
        engine = create_engine(settings.database_url, **kwargs)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
                break
            except TimeoutError:
                pass
            try:
                await asyncio.get_running_loop().run_in_executor(pool, tick, engine, settings)
            except Exception as exc:
                log.warning("HTML shadow paused (%s)", type(exc).__name__)
    except Exception as exc:
        log.warning("HTML shadow unavailable (%s)", type(exc).__name__)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if engine is not None:
            engine.dispose()
