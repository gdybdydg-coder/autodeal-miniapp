"""Pure offline HTTP budget/backoff model for the zero-paid experiment.

No socket, URL, cookie, redirect, database or production integration exists in
this module.  It only models decisions a future shared public-page fetcher would
have to persist before and after bounded read-only requests.
"""

from dataclasses import dataclass, replace
import math
import re


KEY = re.compile(r"[A-Za-z0-9:_-]{1,80}")
RESERVATION = re.compile(r"r[1-9][0-9]{0,11}")
ROUTES = frozenset({"feed_page_1", "feed_page_2", "detail"})
WORK_STATES = frozenset({"queued", "reserved", "complete", "unavailable", "blocked", "exhausted"})
BLOCK_REASONS = frozenset({"http_denied"})
MAX_ROWS = 10_000
MAX_BODY_LIMIT = 5 * 1024 * 1024
MAX_REPORTED_BODY = MAX_BODY_LIMIT + 1
SAFE_INVALID_RETRY_AFTER = 86_400
REDIRECT_PAUSE = 86_400


class HttpBudgetError(ValueError):
    """Stable error code; never includes page, listing or response data."""


@dataclass(frozen=True)
class SourcePermission:
    terms_approved: bool
    robots_allowed: bool
    public_paths_allowed: bool


@dataclass(frozen=True)
class HttpPolicy:
    max_queue: int
    max_inflight: int
    hourly_requests: int
    daily_requests: int
    hourly_bytes: int
    daily_bytes: int
    max_body_bytes: int
    max_attempts: int
    base_backoff_seconds: int
    restart_backoff_seconds: int


@dataclass(frozen=True)
class Work:
    key: str
    route: str
    sequence: int
    state: str
    attempts: int
    available_at: float
    last_result: str | None = None


@dataclass(frozen=True)
class LedgerEntry:
    reservation_id: str
    key: str
    reserved_at: float
    charged_bytes: int
    settled: bool


@dataclass(frozen=True)
class HttpState:
    works: tuple[Work, ...] = ()
    ledger: tuple[LedgerEntry, ...] = ()
    next_sequence: int = 1
    next_reservation: int = 1
    paused_until: float = 0.0
    source_blocked: str | None = None
    queue_overflow: int = 0

    def as_dict(self):
        return {
            "works": [vars(row) for row in self.works],
            "ledger": [vars(row) for row in self.ledger],
            "next_sequence": self.next_sequence,
            "next_reservation": self.next_reservation,
            "paused_until": self.paused_until,
            "source_blocked": self.source_blocked,
            "queue_overflow": self.queue_overflow,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"works", "ledger", "next_sequence", "next_reservation",
                    "paused_until", "source_blocked", "queue_overflow"}
        if not isinstance(value, dict) or set(value) != expected:
            raise HttpBudgetError("invalid_state")
        try:
            state = cls(
                works=tuple(Work(**row) for row in value["works"]),
                ledger=tuple(LedgerEntry(**row) for row in value["ledger"]),
                next_sequence=value["next_sequence"],
                next_reservation=value["next_reservation"],
                paused_until=value["paused_until"],
                source_blocked=value["source_blocked"],
                queue_overflow=value["queue_overflow"],
            )
        except (KeyError, TypeError):
            raise HttpBudgetError("invalid_state") from None
        _validate_state(state)
        return _ordered(state)


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    reason: str
    reservation_id: str | None = None
    key: str | None = None
    route: str | None = None
    method: str | None = None
    allow_redirects: bool | None = None
    cookies_allowed: bool | None = None
    max_body_bytes: int | None = None


def _positive_int(value):
    return type(value) is int and value > 0


def _timestamp(value, *, allow_zero=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value >= 0 if allow_zero else value > 0))


def _validate_permission(permission):
    if not isinstance(permission, SourcePermission) or any(
            type(value) is not bool for value in vars(permission).values()):
        raise HttpBudgetError("invalid_permission")


def _validate_policy(policy):
    if not isinstance(policy, HttpPolicy):
        raise HttpBudgetError("invalid_policy")
    integers = vars(policy)
    if any(not _positive_int(value) for value in integers.values()):
        raise HttpBudgetError("invalid_policy")
    if (policy.max_queue > MAX_ROWS or policy.max_inflight > 16
            or policy.hourly_requests > policy.daily_requests
            or policy.hourly_bytes > policy.daily_bytes
            or policy.max_body_bytes > MAX_BODY_LIMIT
            or policy.max_body_bytes > policy.hourly_bytes
            or policy.hourly_bytes > policy.daily_bytes
            or policy.max_attempts > 10
            or policy.base_backoff_seconds > 86_400
            or policy.restart_backoff_seconds > 86_400):
        raise HttpBudgetError("invalid_policy")


def _validate_state(state):
    if not isinstance(state, HttpState):
        raise HttpBudgetError("invalid_state")
    if (not isinstance(state.works, tuple) or not isinstance(state.ledger, tuple)
            or len(state.works) > MAX_ROWS or len(state.ledger) > MAX_ROWS
            or not _positive_int(state.next_sequence)
            or not _positive_int(state.next_reservation)
            or not _timestamp(state.paused_until, allow_zero=True)
            or state.source_blocked not in (None, *BLOCK_REASONS)
            or type(state.queue_overflow) is not int or state.queue_overflow < 0):
        raise HttpBudgetError("invalid_state")
    if any(not isinstance(row, Work) for row in state.works) or any(
            not isinstance(row, LedgerEntry) for row in state.ledger):
        raise HttpBudgetError("invalid_state")
    if len({row.key for row in state.works}) != len(state.works) or len(
            {row.sequence for row in state.works}) != len(state.works):
        raise HttpBudgetError("invalid_state")
    if any(not KEY.fullmatch(row.key) or row.route not in ROUTES
           or not _positive_int(row.sequence) or row.state not in WORK_STATES
           or type(row.attempts) is not int or not 0 <= row.attempts <= 10
           or not _timestamp(row.available_at) or
           (row.last_result is not None and (not isinstance(row.last_result, str)
                                             or len(row.last_result) > 40))
           for row in state.works):
        raise HttpBudgetError("invalid_state")
    keys = {row.key for row in state.works}
    if len({row.reservation_id for row in state.ledger}) != len(state.ledger) or any(
            not RESERVATION.fullmatch(row.reservation_id) or row.key not in keys
            or not _timestamp(row.reserved_at) or type(row.charged_bytes) is not int
            or not 0 <= row.charged_bytes <= MAX_BODY_LIMIT or type(row.settled) is not bool
            for row in state.ledger):
        raise HttpBudgetError("invalid_state")
    inflight_keys = {row.key for row in state.works if row.state == "reserved"}
    unsettled_keys = {row.key for row in state.ledger if not row.settled}
    if inflight_keys != unsettled_keys:
        raise HttpBudgetError("invalid_state")


def _ordered(state):
    return replace(
        state,
        works=tuple(sorted(state.works, key=lambda row: row.sequence)),
        ledger=tuple(sorted(state.ledger, key=lambda row: int(row.reservation_id[1:]))),
    )


def enqueue(state, key, route, now, policy):
    """Queue one shared unit of work; a repeated key never creates a second row."""
    _validate_state(state)
    _validate_policy(policy)
    if not isinstance(key, str) or not KEY.fullmatch(key) or route not in ROUTES or not _timestamp(now):
        raise HttpBudgetError("invalid_work")
    if key in {row.key for row in state.works}:
        return state, False
    active = sum(row.state in {"queued", "reserved"} for row in state.works)
    if active >= policy.max_queue or len(state.works) >= MAX_ROWS:
        return replace(state, queue_overflow=state.queue_overflow + 1), False
    row = Work(key, route, state.next_sequence, "queued", 0, float(now))
    result = replace(state, works=state.works + (row,), next_sequence=state.next_sequence + 1)
    return _ordered(result), True


def _permission_reason(permission):
    if not permission.terms_approved:
        return "terms_unapproved"
    if not permission.robots_allowed:
        return "robots_disallowed"
    if not permission.public_paths_allowed:
        return "path_not_approved"
    return None


def _usage(state, now, seconds):
    entries = [row for row in state.ledger if row.reserved_at > now - seconds]
    return len(entries), sum(row.charged_bytes for row in entries)


def reserve_next(state, now, permission, policy):
    """Persist request and worst-case byte charges before hypothetical I/O."""
    _validate_state(state)
    _validate_permission(permission)
    _validate_policy(policy)
    if not _timestamp(now):
        raise HttpBudgetError("invalid_time")
    denied = _permission_reason(permission)
    if denied:
        return state, ReservationDecision(False, denied)
    if state.source_blocked:
        return state, ReservationDecision(False, state.source_blocked)
    if now < state.paused_until:
        return state, ReservationDecision(False, "source_backoff")
    if sum(row.state == "reserved" for row in state.works) >= policy.max_inflight:
        return state, ReservationDecision(False, "inflight_limit")
    if len(state.ledger) >= MAX_ROWS:
        return state, ReservationDecision(False, "ledger_capacity")
    due = [row for row in state.works if row.state == "queued" and row.available_at <= now
           and row.attempts < policy.max_attempts]
    if not due:
        return state, ReservationDecision(False, "nothing_due")
    hourly_requests, hourly_bytes = _usage(state, now, 3600)
    daily_requests, daily_bytes = _usage(state, now, 86_400)
    if hourly_requests >= policy.hourly_requests:
        return state, ReservationDecision(False, "hourly_request_budget")
    if daily_requests >= policy.daily_requests:
        return state, ReservationDecision(False, "daily_request_budget")
    if hourly_bytes + policy.max_body_bytes > policy.hourly_bytes:
        return state, ReservationDecision(False, "hourly_byte_budget")
    if daily_bytes + policy.max_body_bytes > policy.daily_bytes:
        return state, ReservationDecision(False, "daily_byte_budget")
    work = min(due, key=lambda row: row.sequence)
    reservation_id = f"r{state.next_reservation}"
    works = tuple(replace(row, state="reserved", attempts=row.attempts + 1,
                          last_result="reserved") if row.key == work.key else row
                  for row in state.works)
    ledger = state.ledger + (LedgerEntry(reservation_id, work.key, float(now),
                                         policy.max_body_bytes, False),)
    result = _ordered(replace(state, works=works, ledger=ledger,
                              next_reservation=state.next_reservation + 1))
    return result, ReservationDecision(
        True, "reserved", reservation_id, work.key, work.route, "GET", False, False,
        policy.max_body_bytes,
    )


def _backoff(policy, attempts):
    return min(86_400, policy.base_backoff_seconds * (2 ** max(0, attempts - 1)))


def _retry(work, now, delay, result, policy):
    if work.attempts >= policy.max_attempts:
        return replace(work, state="exhausted", last_result=result)
    return replace(work, state="queued", available_at=float(now + delay), last_result=result)


def _settle(state, reservation_id, charged_bytes):
    found = False
    ledger = []
    for row in state.ledger:
        if row.reservation_id == reservation_id:
            if row.settled:
                raise HttpBudgetError("reservation_not_inflight")
            row = replace(row, charged_bytes=charged_bytes, settled=True)
            found = True
        ledger.append(row)
    if not found:
        raise HttpBudgetError("reservation_not_inflight")
    return tuple(ledger)


def _reserved_work(state, reservation_id):
    entry = next((row for row in state.ledger if row.reservation_id == reservation_id), None)
    work = next((row for row in state.works if entry and row.key == entry.key), None)
    if entry is None or entry.settled or work is None or work.state != "reserved":
        raise HttpBudgetError("reservation_not_inflight")
    return work


def record_response(state, reservation_id, now, status, body_bytes, content_type,
                    retry_after_seconds, policy):
    """Settle a synthetic response without retaining body, headers or URL data."""
    _validate_state(state)
    _validate_policy(policy)
    if (not isinstance(reservation_id, str) or not RESERVATION.fullmatch(reservation_id)
            or not _timestamp(now) or type(status) is not int or not 100 <= status <= 599
            or type(body_bytes) is not int or not 0 <= body_bytes <= MAX_REPORTED_BODY
            or not isinstance(content_type, str) or len(content_type) > 120
            or (retry_after_seconds is not None and type(retry_after_seconds) is not int)):
        raise HttpBudgetError("invalid_response")
    work = _reserved_work(state, reservation_id)
    charged = min(body_bytes, policy.max_body_bytes)
    ledger = _settle(state, reservation_id, charged)
    paused_until = state.paused_until
    source_blocked = state.source_blocked
    if status in {401, 403, 451}:
        work = replace(work, state="blocked", last_result="http_denied")
        source_blocked = "http_denied"
    elif 300 <= status <= 399:
        work = replace(work, state="blocked", last_result="redirect_denied")
        paused_until = max(paused_until, float(now + REDIRECT_PAUSE))
    elif status == 429:
        delay = (retry_after_seconds if retry_after_seconds is not None
                 and 1 <= retry_after_seconds <= 604_800 else SAFE_INVALID_RETRY_AFTER)
        work = _retry(work, now, delay, "rate_limited", policy)
        paused_until = max(paused_until, float(now + delay))
    elif body_bytes > policy.max_body_bytes:
        work = _retry(work, now, _backoff(policy, work.attempts), "body_too_large", policy)
    elif status == 200 and content_type.lower().split(";", 1)[0].strip() == "text/html":
        work = replace(work, state="complete", last_result="accepted")
    elif status in {404, 410}:
        work = replace(work, state="unavailable", last_result="not_available")
    elif status >= 500:
        work = _retry(work, now, _backoff(policy, work.attempts), "upstream_error", policy)
    elif status == 200:
        work = _retry(work, now, _backoff(policy, work.attempts), "unexpected_content", policy)
    else:
        work = replace(work, state="unavailable", last_result="client_error")
    works = tuple(work if row.key == work.key else row for row in state.works)
    result = _ordered(replace(state, works=works, ledger=ledger,
                              paused_until=paused_until, source_blocked=source_blocked))
    _validate_state(result)
    return result


def record_transport_error(state, reservation_id, now, policy):
    """Count the full reservation and schedule a bounded retry after an I/O error."""
    _validate_state(state)
    _validate_policy(policy)
    if not isinstance(reservation_id, str) or not RESERVATION.fullmatch(reservation_id) or not _timestamp(now):
        raise HttpBudgetError("invalid_transport_error")
    work = _reserved_work(state, reservation_id)
    ledger = _settle(state, reservation_id, policy.max_body_bytes)
    work = _retry(work, now, _backoff(policy, work.attempts), "transport_error", policy)
    works = tuple(work if row.key == work.key else row for row in state.works)
    result = _ordered(replace(state, works=works, ledger=ledger))
    _validate_state(result)
    return result


def recover_after_restart(state, now, policy):
    """Never refund a reserved request; retry GET only after a durable delay."""
    _validate_state(state)
    _validate_policy(policy)
    if not _timestamp(now):
        raise HttpBudgetError("invalid_time")
    reserved_keys = {row.key for row in state.works if row.state == "reserved"}
    works = []
    for row in state.works:
        if row.key not in reserved_keys:
            works.append(row)
        elif row.attempts >= policy.max_attempts:
            works.append(replace(row, state="exhausted", last_result="restart_uncertain"))
        else:
            works.append(replace(row, state="queued", available_at=float(now + policy.restart_backoff_seconds),
                                 last_result="restart_uncertain"))
    ledger = tuple(replace(row, settled=True) if not row.settled else row for row in state.ledger)
    result = _ordered(replace(state, works=tuple(works), ledger=ledger))
    _validate_state(result)
    return result
