"""Offline composition of the isolated zero-paid discovery components.

Caller-supplied sanitized HTML is the only input.  This module performs no
network, database, backend, paid API, AI or Telegram operation and cannot send.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import math
import re

from experiments.free_search.filter_gate import SearchFilter, gate
from experiments.free_search.http_budget import (
    HttpPolicy, HttpState, SourcePermission, record_response,
    recover_after_restart as recover_http, reserve_next,
    enqueue as enqueue_http,
)
from experiments.free_search.offline_queue import (
    QueueState, begin_evaluation, complete_evaluation, queue_candidate,
    recover_after_restart as recover_queue,
)
from experiments.free_search.public_cards import (
    Candidate, PublicationState, advance_publications, parse_public_cards,
)
from experiments.free_search.public_details import ParseError, parse_public_details
from experiments.free_search.visible_adapter import (
    VisibleParseError, adapt_candidate, parse_visible_facts,
)


ID = re.compile(r"[1-9][0-9]{0,11}")
KEY = re.compile(r"detail:[1-9][0-9]{0,11}:[1-9][0-9]{0,19}")
MAX_PIPELINE_ROWS = 1000


class PipelineError(ValueError):
    """Stable error code; never contains source HTML or user data."""


@dataclass(frozen=True)
class PendingDetail:
    key: str
    candidate: Candidate


@dataclass(frozen=True)
class ResearchCandidate:
    listing_id: str
    publication_at: float
    asking_price_usd: Decimal
    matching_search_ids: tuple[int, ...]


@dataclass(frozen=True)
class PipelineState:
    publications: PublicationState = PublicationState(None)
    http: HttpState = HttpState()
    queue: QueueState = QueueState()
    pending_details: tuple[PendingDetail, ...] = ()
    research_candidates: tuple[ResearchCandidate, ...] = ()

    def as_dict(self):
        return {
            "publications": self.publications.as_dict(),
            "http": self.http.as_dict(),
            "queue": self.queue.as_dict(),
            "pending_details": [{
                "key": row.key,
                "candidate": {
                    "listing_id": row.candidate.listing_id,
                    "url": row.candidate.url,
                    "added_at": row.candidate.added_at,
                    "preview_usd": (None if row.candidate.preview_usd is None
                                    else str(row.candidate.preview_usd)),
                    "evidence": row.candidate.evidence,
                },
            } for row in self.pending_details],
            "research_candidates": [{
                "listing_id": row.listing_id,
                "publication_at": row.publication_at,
                "asking_price_usd": str(row.asking_price_usd),
                "matching_search_ids": list(row.matching_search_ids),
            } for row in self.research_candidates],
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"publications", "http", "queue", "pending_details", "research_candidates"}
        if not isinstance(value, dict) or set(value) != expected:
            raise PipelineError("invalid_state")
        try:
            pending = []
            for row in value["pending_details"]:
                raw = row["candidate"]
                preview = None if raw["preview_usd"] is None else Decimal(raw["preview_usd"])
                candidate = Candidate(raw["listing_id"], raw["url"], raw["added_at"],
                                      preview, raw["evidence"])
                pending.append(PendingDetail(row["key"], candidate))
            research = tuple(ResearchCandidate(
                row["listing_id"], row["publication_at"], Decimal(row["asking_price_usd"]),
                tuple(row["matching_search_ids"]),
            ) for row in value["research_candidates"])
            state = cls(
                PublicationState.from_dict(value["publications"]),
                HttpState.from_dict(value["http"]),
                QueueState.from_dict(value["queue"]),
                tuple(pending), research,
            )
        except (KeyError, TypeError, InvalidOperation, ValueError):
            raise PipelineError("invalid_state") from None
        _validate(state)
        return _ordered(state)


@dataclass(frozen=True)
class IngestOutcome:
    baseline_only: bool
    queued: int
    reason: str


@dataclass(frozen=True)
class DetailOutcome:
    processed: bool
    reason: str
    matching_search_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResearchOutcome:
    processed: bool
    reason: str
    discount_percent: Decimal | None = None
    qualifying_search_ids: tuple[int, ...] = ()
    claims_created: int = 0


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _decimal(value, *, positive=False):
    return (isinstance(value, Decimal) and value.is_finite()
            and (value > 0 if positive else True))


def _candidate_key(candidate):
    micros = int(candidate.added_at * 1_000_000)
    return f"detail:{candidate.listing_id}:{micros}"


def _valid_candidate(candidate):
    return (isinstance(candidate, Candidate) and ID.fullmatch(candidate.listing_id)
            and isinstance(candidate.url, str)
            and re.fullmatch(r"https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_"
                             + re.escape(candidate.listing_id) + r"\.html", candidate.url)
            and _time(candidate.added_at)
            and (candidate.preview_usd is None or _decimal(candidate.preview_usd, positive=True))
            and candidate.evidence == "public_card_add_date")


def _ordered(state):
    return replace(
        state,
        pending_details=tuple(sorted(state.pending_details, key=lambda row: row.key)),
        research_candidates=tuple(sorted(state.research_candidates,
                                         key=lambda row: (row.publication_at, row.listing_id))),
    )


def _validate(state):
    if not isinstance(state, PipelineState):
        raise PipelineError("invalid_state")
    if (not isinstance(state.publications, PublicationState)
            or not isinstance(state.http, HttpState) or not isinstance(state.queue, QueueState)
            or not isinstance(state.pending_details, tuple)
            or not isinstance(state.research_candidates, tuple)
            or len(state.pending_details) > MAX_PIPELINE_ROWS
            or len(state.research_candidates) > MAX_PIPELINE_ROWS
            or any(not isinstance(row, PendingDetail) for row in state.pending_details)
            or any(not isinstance(row, ResearchCandidate) for row in state.research_candidates)):
        raise PipelineError("invalid_state")
    pending_keys = {row.key for row in state.pending_details}
    if len(pending_keys) != len(state.pending_details) or any(
            not KEY.fullmatch(row.key) or not _valid_candidate(row.candidate)
            or row.key != _candidate_key(row.candidate) for row in state.pending_details):
        raise PipelineError("invalid_state")
    http_rows = {row.key: row for row in state.http.works}
    if any(key not in http_rows or http_rows[key].route != "detail" for key in pending_keys):
        raise PipelineError("invalid_state")
    research_ids = {row.listing_id for row in state.research_candidates}
    if len(research_ids) != len(state.research_candidates) or any(
            not ID.fullmatch(row.listing_id) or not _time(row.publication_at)
            or not _decimal(row.asking_price_usd, positive=True)
            or not isinstance(row.matching_search_ids, tuple) or not row.matching_search_ids
            or len(set(row.matching_search_ids)) != len(row.matching_search_ids)
            or any(type(search_id) is not int or search_id <= 0 for search_id in row.matching_search_ids)
            for row in state.research_candidates):
        raise PipelineError("invalid_state")
    if research_ids & {row.candidate.listing_id for row in state.pending_details}:
        raise PipelineError("invalid_state")


def ingest_feed_fixture(state, html, observed_at, policy):
    """Parse one supplied feed fixture and transactionally queue detail work."""
    _validate(state)
    cards = parse_public_cards(html)
    publications, candidates = advance_publications(state.publications, cards, observed_at)
    if state.publications.baseline_at is None:
        result = _ordered(replace(state, publications=publications))
        return result, IngestOutcome(True, 0, "baseline")
    if not candidates:
        return _ordered(replace(state, publications=publications)), IngestOutcome(False, 0, "no_candidates")
    trial_http = state.http
    trial_pending = list(state.pending_details)
    existing_pending = {row.key for row in trial_pending}
    queued = 0
    for candidate in candidates:
        key = _candidate_key(candidate)
        trial_http, added = enqueue_http(trial_http, key, "detail", observed_at, policy)
        if not added and key not in {row.key for row in trial_http.works}:
            # Roll publication progress back so the next snapshot may retry it.
            rolled_http = replace(state.http, queue_overflow=trial_http.queue_overflow)
            return _ordered(replace(state, http=rolled_http)), IngestOutcome(
                False, 0, "detail_queue_full")
        if key not in existing_pending:
            trial_pending.append(PendingDetail(key, candidate))
            existing_pending.add(key)
        if added:
            queued += 1
    result = _ordered(replace(state, publications=publications, http=trial_http,
                              pending_details=tuple(trial_pending)))
    _validate(result)
    return result, IngestOutcome(False, queued, "queued")


def reserve_detail(state, now, permission, policy):
    _validate(state)
    http, decision = reserve_next(state.http, now, permission, policy)
    return _ordered(replace(state, http=http)), decision


def _reservation_key(state, reservation_id):
    entry = next((row for row in state.http.ledger if row.reservation_id == reservation_id), None)
    return None if entry is None else entry.key


def _validate_bindings(state, bindings):
    if (not isinstance(bindings, dict)
            or any(type(key) is not int or key <= 0 or not isinstance(value, SearchFilter)
                   for key, value in bindings.items())):
        raise PipelineError("invalid_bindings")
    subscription_ids = {row.search_id for row in state.queue.subscriptions}
    if not set(bindings).issubset(subscription_ids):
        raise PipelineError("invalid_bindings")


def complete_detail_fixture(state, reservation_id, now, page_html, bindings, policy,
                            status=200, content_type="text/html", retry_after_seconds=None):
    """Settle one synthetic detail response, parse once and fan out filter checks."""
    _validate(state)
    _validate_bindings(state, bindings)
    key = _reservation_key(state, reservation_id)
    pending = next((row for row in state.pending_details if row.key == key), None)
    if pending is None or not isinstance(page_html, str):
        raise PipelineError("invalid_detail_completion")
    body_bytes = len(page_html.encode("utf-8"))
    reported = min(body_bytes, 5 * 1024 * 1024 + 1)
    http = record_response(state.http, reservation_id, now, status, reported,
                           content_type, retry_after_seconds, policy)
    work = next(row for row in http.works if row.key == key)
    if work.state != "complete":
        terminal = work.state in {"unavailable", "blocked", "exhausted"}
        rows = tuple(row for row in state.pending_details if row.key != key) if terminal else state.pending_details
        result = _ordered(replace(state, http=http, pending_details=rows))
        return result, DetailOutcome(False, work.last_result or work.state)
    candidate = pending.candidate
    try:
        details = parse_public_details(page_html, candidate.listing_id)
        visible = parse_visible_facts(page_html, candidate.listing_id)
        adapted = adapt_candidate(candidate, details, visible)
    except (ParseError, VisibleParseError):
        rows = tuple(row for row in state.pending_details if row.key != key)
        return _ordered(replace(state, http=http, pending_details=rows)), DetailOutcome(
            False, "fixture_rejected")
    matches = tuple(sorted(search_id for search_id, filters in bindings.items()
                           if gate(adapted.evidence, filters).eligible_for_valuation))
    rows = tuple(row for row in state.pending_details if row.key != key)
    ready = state.research_candidates
    if matches:
        ready += (ResearchCandidate(candidate.listing_id, candidate.added_at,
                                    adapted.evidence.price_usd, matches),)
    result = _ordered(replace(state, http=http, pending_details=rows,
                              research_candidates=ready))
    _validate(result)
    return result, DetailOutcome(True, "eligible" if matches else "no_matching_filters", matches)


def complete_research_estimate(state, listing_id, reference_price_usd, confirmed,
                               bindings, max_jobs=128, max_claims=512):
    """Apply one caller-supplied research estimate; never perform or send it."""
    _validate(state)
    _validate_bindings(state, bindings)
    if (not isinstance(listing_id, str) or not ID.fullmatch(listing_id)
            or not _decimal(reference_price_usd, positive=True) or type(confirmed) is not bool):
        raise PipelineError("invalid_research_estimate")
    item = next((row for row in state.research_candidates if row.listing_id == listing_id), None)
    if item is None:
        raise PipelineError("research_candidate_missing")
    queue = queue_candidate(state.queue, item.listing_id, item.publication_at, max_jobs=max_jobs)
    if item.listing_id not in {row.listing_id for row in queue.jobs}:
        return _ordered(replace(state, queue=queue)), ResearchOutcome(False, "job_queue_full")
    discount = ((reference_price_usd - item.asking_price_usd) / reference_price_usd
                * Decimal("100"))
    qualifying = ()
    if confirmed:
        qualifying = tuple(sorted(search_id for search_id in item.matching_search_ids
                                  if search_id in bindings
                                  and discount >= bindings[search_id].min_discount_percent))
    queue, snapshot = begin_evaluation(queue, item.listing_id,
                                       qualifying if confirmed else item.matching_search_ids)
    before = len(queue.claims)
    queue = complete_evaluation(queue, snapshot, confirmed, max_claims=max_claims)
    ready = tuple(row for row in state.research_candidates if row.listing_id != listing_id)
    result = _ordered(replace(state, queue=queue, research_candidates=ready))
    _validate(result)
    return result, ResearchOutcome(True, "confirmed" if confirmed else "unconfirmed",
                                   discount, qualifying, len(queue.claims) - before)


def recover_pipeline_after_restart(state, now, policy):
    """Round-trip helper: preserve candidates and recover both bounded machines."""
    _validate(state)
    result = _ordered(replace(state, http=recover_http(state.http, now, policy),
                              queue=recover_queue(state.queue)))
    _validate(result)
    return result
