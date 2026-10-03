"""Sanitized, prospective accounting: reservations are not provider charges.

Only adapters that emit ``transport_started`` prove dispatch to the HTTP opener.
An interrupted process may leave a reserved/dispatched/started row incomplete;
never infer success, exact provider acceptance, or billable units from it.
"""
import re
import hashlib
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Boolean, Float, Index, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .models import Base


ERRORS = frozenset({"key_rejected", "access_denied", "quota_exceeded", "upstream_error",
    "connection_error", "invalid_response", "listing_unavailable", "info_endpoint_unavailable",
    "ai_not_configured", "ai_access_denied", "ai_upstream_error", "ai_connection_error",
    "ai_invalid_response", "search_limit", "validation_limit", "validation_stopped"})
COMPLETE = frozenset({"success", "error"})
_ACTIVE_OBSERVER = ContextVar("ria_api_attempt_observer", default=None)


def current_observer():
    """Internal thread/task-local callback; never carries raw request data."""
    return _ACTIVE_OBSERVER.get()


@contextmanager
def active_observer(callback):
    token = _ACTIVE_OBSERVER.set(callback)
    try:
        yield
    finally:
        _ACTIVE_OBSERVER.reset(token)


class RiaApiAttempt(Base):
    __tablename__ = "ria_api_attempts"
    __table_args__ = (Index("ria_attempt_window_category", "reserved_at", "category"),
                     Index("ria_attempt_transport_category", "transport_started_at", "category"),
                     Index("ria_attempt_fingerprint_time", "request_fingerprint", "reserved_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(24))
    reserved_at: Mapped[float] = mapped_column(Float)
    dispatched_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    transport_started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="reserved")
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_relation: Mapped[str] = mapped_column(String(32))
    request_ordinal: Mapped[int] = mapped_column(Integer)
    prior_attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    forced: Mapped[bool] = mapped_column(Boolean)


class RiaApiAuthorization(Base):
    """Additive table; old attempt rows and payment schemas remain unchanged."""
    __tablename__ = "ria_api_authorizations"
    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    reason: Mapped[str] = mapped_column(String(40))
    group_fingerprint: Mapped[str] = mapped_column(String(64))


REASONS = frozenset({"publication_search", "candidate_evaluation", "active_window_search",
    "recent_publication_validation", "user_requested_scan", "authenticated_search",
    "authenticated_catalog", "paid_receipt_photo_repair"})


def authorization(reason, group):
    if reason not in REASONS or not isinstance(group, str) or not group:
        raise ValueError("invalid_source_authorization_context")
    return {"reason": reason, "group_fingerprint": hashlib.sha256(group.encode()).hexdigest()}


def category(path):
    if path == "search":
        return "search"
    if path == "info":
        return "detail"
    if path == "ai-avarage-price":
        return "valuation"
    if (path in {"states", "type", "categories/1/marks", "categories/1/bodystyles",
                 "categories/1/gearboxes"}
            or re.fullmatch(r"categories/1/marks/[1-9][0-9]*/models", path)
            or re.fullmatch(r"modifications/by/generation/[1-9][0-9]*/body/[1-9][0-9]*/modifications", path)):
        return "catalog"
    return "other"


def reserve(db, path, fingerprint, now, *, force, authorization_context=None):
    """Same transaction as SourceBudget. No raw path, parameters or IDs stored."""
    previous = db.scalar(select(RiaApiAttempt).where(
        RiaApiAttempt.request_fingerprint == fingerprint).order_by(
            RiaApiAttempt.request_ordinal.desc()).limit(1))
    relation = ("first_observed" if previous is None else
                "repeat_after_failure" if previous.state == "error" else
                "repeat_after_success" if previous.state == "success" else "repeat_after_incomplete")
    attempt = RiaApiAttempt(id=uuid.uuid4().hex, request_fingerprint=fingerprint,
        category=category(path), reserved_at=now, state="reserved", call_relation=relation,
        request_ordinal=previous.request_ordinal + 1 if previous else 1,
        prior_attempt_id=previous.id if previous else None, forced=bool(force))
    db.add(attempt)
    if authorization_context is not None:
        reason = authorization_context.get("reason")
        group = authorization_context.get("group_fingerprint")
        if reason not in REASONS or not isinstance(group, str) or not re.fullmatch(r"[0-9a-f]{64}", group):
            raise ValueError("invalid_source_authorization_context")
        db.add(RiaApiAuthorization(attempt_id=attempt.id, reason=reason, group_fingerprint=group))
    return attempt.id


def update(engine, attempt_id, phase, *, clock=time.time, http_status=None, error_code=None):
    """Fixed fields only. Adapter failures are reported independently of parsing."""
    with Session(engine) as db:
        row = db.get(RiaApiAttempt, attempt_id)
        if row is None:
            return
        now = clock()
        if phase == "dispatched":
            row.dispatched_at, row.state = now, "dispatched"
        elif phase == "transport_started":
            row.transport_started_at, row.state = now, "transport_started"
        elif phase == "http_response":
            if type(http_status) is int and 100 <= http_status <= 599:
                row.http_status = http_status
        elif phase == "transport_failed":
            # Completion code arrives from the caller; do not invent a status.
            pass
        elif phase in COMPLETE:
            row.finished_at, row.state = now, phase
            row.error_code = (error_code if error_code in ERRORS else "operation_error") if phase == "error" else None
        else:
            return
        db.commit()


def observer(engine, attempt_id, clock):
    def record(event):
        if not isinstance(event, dict):
            return
        phase = event.get("event")
        if phase in {"transport_started", "http_response", "transport_failed"}:
            update(engine, attempt_id, phase, clock=clock, http_status=event.get("http_status"))
    return record


def summary(db, after, before):
    """Aggregate one half-open interval; no inferred historical backfill."""
    criteria = (RiaApiAttempt.reserved_at >= after, RiaApiAttempt.reserved_at < before)
    grouped = db.execute(select(RiaApiAttempt.category, RiaApiAttempt.state,
        RiaApiAttempt.call_relation, func.count()).where(*criteria).group_by(
            RiaApiAttempt.category, RiaApiAttempt.state, RiaApiAttempt.call_relation))
    categories, states, relations = {}, {}, {}
    for kind, state, relation, count in grouped:
        categories[kind] = categories.get(kind, 0) + count
        states[state] = states.get(state, 0) + count
        relations[relation] = relations.get(relation, 0) + count
    statuses = {str(code): count for code, count in db.execute(select(
        RiaApiAttempt.http_status, func.count()).where(*criteria,
            RiaApiAttempt.http_status.is_not(None)).group_by(RiaApiAttempt.http_status))}
    # An operation can be reserved just before a boundary and reach the
    # transport after it. Count outgoing attempts by their own timestamp.
    transport_criteria = (RiaApiAttempt.transport_started_at >= after,
                          RiaApiAttempt.transport_started_at < before)
    transported = db.scalar(select(func.count()).select_from(RiaApiAttempt).where(
        *transport_criteria)) or 0
    transport_categories = dict(db.execute(select(RiaApiAttempt.category, func.count()).where(
        *transport_criteria).group_by(RiaApiAttempt.category)).all())
    transport_relations = dict(db.execute(select(RiaApiAttempt.call_relation, func.count()).where(
        *transport_criteria).group_by(RiaApiAttempt.call_relation)).all())
    transport_breakdown = [
        {"category": kind, "call_relation": relation, "attempts": count}
        for kind, relation, count in db.execute(select(RiaApiAttempt.category,
            RiaApiAttempt.call_relation, func.count()).where(*transport_criteria)
            .group_by(RiaApiAttempt.category, RiaApiAttempt.call_relation))]
    dispatched = db.scalar(select(func.count()).select_from(RiaApiAttempt).where(
        *criteria, RiaApiAttempt.dispatched_at.is_not(None))) or 0
    authorized = db.execute(select(RiaApiAuthorization.reason, func.count(),
            func.count(func.distinct(RiaApiAuthorization.group_fingerprint)))
        .join(RiaApiAttempt, RiaApiAttempt.id == RiaApiAuthorization.attempt_id)
        .where(*transport_criteria).group_by(RiaApiAuthorization.reason)).all()
    return {"window": {"after_inclusive": after, "before_exclusive": before},
        "reservations": sum(states.values()), "dispatched_operations": dispatched,
        "transport_observed_attempts": transported, "states": states, "categories": categories,
        "categories_basis": "reservations", "transport_by_category": transport_categories,
        "transport_window_basis": "transport_started_at", "transport_call_relations": transport_relations,
        "transport_breakdown": transport_breakdown,
        "call_relations": relations, "http_statuses": statuses,
        "transport_authorization": [{"reason": reason, "attempts": count, "distinct_groups": groups}
                                    for reason, count, groups in authorized],
        "transport_without_authorization_context": transported - sum(count for _, count, _ in authorized),
        "incomplete": sum(count for state, count in states.items() if state not in COMPLETE),
        "cache_hits_in_attempts": 0, "cache_hit_calls": None, "provider_charged_units": None,
        "scope": "new_source_budget_reservations_only", "historical_backfill": False}
