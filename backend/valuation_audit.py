"""Operator-only replay of retained evidence: no provider calls, jobs or sends.

Replays at the original evaluation time, never as a current valuation. The
one-time durable claim only writes a SourceProbe; it cannot reset delivery or
monitor state. Private subscription data and raw listing fields are not logged.
"""
import json
import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import MonitorJob, SourceProbe
from .reference_valuation import notification_estimate

LIMIT = 20
PUBLIC_FIELDS = ("id", "brand", "model", "year", "price_usd", "mileage", "engine_cc")


def validate_run_id(run_id):
    if run_id and not re.fullmatch(r"[a-z0-9-]{1,40}", run_id):
        raise ValueError("Invalid valuation audit ID")


def replay(result):
    rating = result.get("rating", {})
    proof = rating.get("valuation_evidence", {})
    candidate = result.get("candidate", {})
    evaluated_at = proof.get("evaluated_at")
    if not candidate or not isinstance(evaluated_at, (int, float)):
        return None
    # Older exact proofs omitted condition_exclusions, but explicitly true
    # comparable_condition already asserted that all exclusion flags were clear.
    # Use that retained assertion only for this historical, non-deliverable replay.
    peers = [{**peer, "condition_exclusions": peer.get("condition_exclusions",
                 [] if peer.get("comparable_condition") is True else None)}
             for peer in proof.get("peers", [])]
    revised = notification_estimate(candidate, peers, now=evaluated_at)
    return {"replay_only": True, "original_evaluated_at": evaluated_at,
            "car": {key: candidate.get(key) for key in PUBLIC_FIELDS},
            "previous_valuation": rating.get("valuation"),
            "peer_prices_usd": sorted(peer["price_usd"] for peer in peers),
            "revised_valuation": revised["valuation"], "revised_market_usd": revised["market"],
            "revised_discount": revised["discount"], "comparables": revised["comparables"],
            "reference_kind": revised["valuation_evidence"].get("reference_kind")}


def check_once(engine, run_id):
    validate_run_id(run_id)
    if not run_id:
        return
    probe_id = "valuation-audit-" + run_id
    with Session(engine) as db:
        if db.get(SourceProbe, probe_id):
            return
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
        records = list(db.scalars(select(MonitorJob.result).where(
            MonitorJob.first_seen >= time.time() - 86400,
            MonitorJob.result["rating"]["valuation"].as_string() == "mixed_sample")
            .order_by(MonitorJob.last_attempt.desc()).limit(LIMIT)))
    rows = []
    for result in records:
        try:
            row = replay(result)
        except (KeyError, TypeError, ValueError, OverflowError):
            row = {"replay_only": True, "error": "invalid_retained_evidence"}
        if row:
            rows.append(row)
            logging.warning("Valuation evidence replay %s", json.dumps(row, ensure_ascii=False, sort_keys=True))
    with Session(engine) as db:
        probe = db.get(SourceProbe, probe_id)
        probe.status, probe.checked_at = "checked", time.time()
        probe.result = {"replay_only": True, "provider_requests": 0, "rows": rows}
        db.commit()
