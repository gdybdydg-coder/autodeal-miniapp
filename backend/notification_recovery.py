"""Explicit operator recovery of one reported listing, through normal checks.

No provider calls, direct sends, public trigger, recipient override or quota reset.
The ordinary monitor resolves current filters, refreshes price, evaluates and sends.
"""
import json
import logging
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Delivery, Listing, MonitorJob, MonitorSeen, SourceProbe
from .notification_diagnostic import validate_id


def probe_id(source_id):
    validate_id(source_id)
    return "notification-recovery-v1-" + source_id


def recover_once(monitor, source_id):
    from .monitor import active_members

    if not source_id or not monitor.settings.live or not monitor.settings.monitor_enabled:
        return
    claim = probe_id(source_id)
    with Session(monitor.engine) as db:
        if not monitor.owned(db) or db.get(SourceProbe, claim):
            return
        members = active_members(db)
        # Never wait for a future user's subscription to become a recipient.
        queued = 0
        listing = db.scalar(select(Listing).where(Listing.source == "auto_ria",
                                                  Listing.source_id == source_id))
        for search, watch, _ in members:
            if not monitor.current(db, search.id, search.user_id, watch.epoch):
                continue
            if listing and db.scalar(select(Delivery.id).where(
                    Delivery.user_id == search.user_id, Delivery.listing_id == listing.id)) is not None:
                # Includes uncertain sends: never force duplicate delivery.
                continue
            seen = db.get(MonitorSeen, (search.id, source_id))
            if seen is None:
                seen = MonitorSeen(search_id=search.id, source_id=source_id,
                                   epoch=watch.epoch, first_seen=time.time())
                db.add(seen)
            if seen.epoch != watch.epoch:
                continue
            seen.state = "pending"
            queued += 1
        if queued:
            job = db.get(MonitorJob, source_id)
            if job is None:
                job = MonitorJob(source_id=source_id, first_seen=time.time())
                db.add(job)
            # Do not authorize sending from a diagnostic cache or old valuation.
            job.state, job.next_run, job.result, job.reason = "pending", 0, {}, ""
        db.add(SourceProbe(id=claim, status="queued" if queued else "no_recipients",
                           checked_at=time.time(), requests=0,
                           result={"source_id": source_id, "queued_subscriptions": queued}))
        db.commit()


def report(engine, source_id):
    """Log state changes only; no subscriber IDs, seller data or credentials."""
    with Session(engine) as db:
        probe = db.get(SourceProbe, probe_id(source_id))
        if not probe:
            return
        job = db.get(MonitorJob, source_id)
        listing = db.scalar(select(Listing).where(Listing.source == "auto_ria",
                                                  Listing.source_id == source_id))
        rating = job.result.get("rating", {}) if job else {}
        report_data = {"source_id": source_id, "job_state": job.state if job else None,
                       "reason": job.reason if job else None,
                       "price_usd": job.result.get("candidate", {}).get("price_usd") if job else None,
                       "valuation": rating.get("valuation"), "market": rating.get("market"),
                       "comparables": rating.get("comparables"),
                       "valuation_reasons": rating.get("valuation_reasons", []),
                       "peer_rejections": dict(Counter(reason for peer in
                           rating.get("valuation_evidence", {}).get("rejected", [])
                           for reason in peer.get("reasons", []))),
                       "delivery_states": dict(Counter(db.scalars(select(Delivery.state).where(
                           Delivery.listing_id == listing.id)))) if listing else {}}
        if probe.result.get("report") == report_data:
            return
        probe.result = {**probe.result, "report": report_data}
        db.commit()
    logging.warning("Notification recovery %s", json.dumps(report_data, sort_keys=True))
