"""Pure offline model of bounded jobs, subscription epochs and delivery claims.

This is deliberately not the production dispatcher.  It performs no network,
database, backend or Telegram work and can never send a message.
"""

from dataclasses import dataclass, replace
import math
import re


ID = re.compile(r"[1-9][0-9]{0,11}")
EPOCH = re.compile(r"[A-Za-z0-9_-]{1,40}")
JOB_STATES = frozenset({"pending", "evaluating", "complete", "unvalued"})
CLAIM_STATES = frozenset({"pending", "sending", "sent", "uncertain", "failed", "cancelled"})
SEEN_STATES = frozenset({"pending", "checked", "unvalued"})
MAX_JOBS = 128
MAX_CLAIMS = 512
MAX_ROWS = 10_000


class QueueStateError(ValueError):
    """Stable error code; never contains user or listing data."""


@dataclass(frozen=True)
class UserState:
    user_id: int
    ready: bool


@dataclass(frozen=True)
class Subscription:
    search_id: int
    user_id: int
    epoch: str
    started_at: float
    enabled: bool


@dataclass(frozen=True)
class Job:
    listing_id: str
    publication_at: float
    state: str = "pending"


@dataclass(frozen=True)
class Seen:
    search_id: int
    listing_id: str
    epoch: str
    state: str = "pending"


@dataclass(frozen=True)
class Claim:
    user_id: int
    listing_id: str
    matches: tuple[tuple[int, str], ...]
    state: str = "pending"


@dataclass(frozen=True)
class EvaluationSnapshot:
    listing_id: str
    publication_at: float
    targets: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class QueueState:
    users: tuple[UserState, ...] = ()
    subscriptions: tuple[Subscription, ...] = ()
    jobs: tuple[Job, ...] = ()
    seen: tuple[Seen, ...] = ()
    claims: tuple[Claim, ...] = ()
    job_overflow: int = 0
    claim_overflow: int = 0

    def as_dict(self):
        return {
            "users": [vars(row) for row in self.users],
            "subscriptions": [vars(row) for row in self.subscriptions],
            "jobs": [vars(row) for row in self.jobs],
            "seen": [vars(row) for row in self.seen],
            "claims": [{**vars(row), "matches": [list(item) for item in row.matches]} for row in self.claims],
            "job_overflow": self.job_overflow,
            "claim_overflow": self.claim_overflow,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
                "users", "subscriptions", "jobs", "seen", "claims", "job_overflow", "claim_overflow"}:
            raise QueueStateError("invalid_state")
        try:
            state = cls(
                users=tuple(UserState(**row) for row in value["users"]),
                subscriptions=tuple(Subscription(**row) for row in value["subscriptions"]),
                jobs=tuple(Job(**row) for row in value["jobs"]),
                seen=tuple(Seen(**row) for row in value["seen"]),
                claims=tuple(Claim(**{**row, "matches": tuple(tuple(item) for item in row["matches"])})
                             for row in value["claims"]),
                job_overflow=value["job_overflow"], claim_overflow=value["claim_overflow"],
            )
        except (TypeError, KeyError):
            raise QueueStateError("invalid_state") from None
        _validate(state)
        return _sorted(state)


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _positive(value):
    return type(value) is int and value > 0


def _valid_match(value):
    return (isinstance(value, tuple) and len(value) == 2
            and _positive(value[0]) and isinstance(value[1], str)
            and EPOCH.fullmatch(value[1]))


def _sorted(state):
    return replace(
        state,
        users=tuple(sorted(state.users, key=lambda row: row.user_id)),
        subscriptions=tuple(sorted(state.subscriptions, key=lambda row: row.search_id)),
        jobs=tuple(sorted(state.jobs, key=lambda row: (row.publication_at, row.listing_id))),
        seen=tuple(sorted(state.seen, key=lambda row: (row.search_id, row.listing_id, row.epoch))),
        claims=tuple(sorted(state.claims, key=lambda row: (row.user_id, row.listing_id))),
    )


def _validate(state):
    if not isinstance(state, QueueState):
        raise QueueStateError("invalid_state")
    rows = (state.users, state.subscriptions, state.jobs, state.seen, state.claims)
    if any(not isinstance(group, tuple) or len(group) > MAX_ROWS for group in rows):
        raise QueueStateError("invalid_state")
    if type(state.job_overflow) is not int or state.job_overflow < 0 or type(state.claim_overflow) is not int or state.claim_overflow < 0:
        raise QueueStateError("invalid_state")
    if len({row.user_id for row in state.users}) != len(state.users) or any(
            not _positive(row.user_id) or type(row.ready) is not bool for row in state.users):
        raise QueueStateError("invalid_state")
    user_ids = {row.user_id for row in state.users}
    if len({row.search_id for row in state.subscriptions}) != len(state.subscriptions) or any(
            not _positive(row.search_id) or row.user_id not in user_ids or not EPOCH.fullmatch(row.epoch)
            or not _time(row.started_at) or type(row.enabled) is not bool for row in state.subscriptions):
        raise QueueStateError("invalid_state")
    if len({row.listing_id for row in state.jobs}) != len(state.jobs) or any(
            not ID.fullmatch(row.listing_id) or not _time(row.publication_at) or row.state not in JOB_STATES
            for row in state.jobs):
        raise QueueStateError("invalid_state")
    if len({(row.search_id, row.listing_id, row.epoch) for row in state.seen}) != len(state.seen) or any(
            not _positive(row.search_id) or not ID.fullmatch(row.listing_id) or not EPOCH.fullmatch(row.epoch)
            or row.state not in SEEN_STATES for row in state.seen):
        raise QueueStateError("invalid_state")
    if len({(row.user_id, row.listing_id) for row in state.claims}) != len(state.claims) or any(
            row.user_id not in user_ids or not ID.fullmatch(row.listing_id) or row.state not in CLAIM_STATES
            or not isinstance(row.matches, tuple) or not row.matches
            or any(not _valid_match(match) for match in row.matches) for row in state.claims):
        raise QueueStateError("invalid_state")


def put_user(state, user_id, ready):
    _validate(state)
    if not _positive(user_id) or type(ready) is not bool:
        raise QueueStateError("invalid_user")
    users = {row.user_id: row for row in state.users}
    users[user_id] = UserState(user_id, ready)
    return _sorted(replace(state, users=tuple(users.values())))


def put_subscription(state, subscription):
    _validate(state)
    if not isinstance(subscription, Subscription):
        raise QueueStateError("invalid_subscription")
    users = {row.user_id for row in state.users}
    if subscription.user_id not in users or not _positive(subscription.search_id) or not EPOCH.fullmatch(subscription.epoch) or not _time(subscription.started_at):
        raise QueueStateError("invalid_subscription")
    rows = {row.search_id: row for row in state.subscriptions}
    rows[subscription.search_id] = subscription
    result = _sorted(replace(state, subscriptions=tuple(rows.values())))
    _validate(result)
    return result


def stop_user(state, user_id):
    """Model /stop: disable subscriptions and cancel only not-yet-attempted claims."""
    _validate(state)
    if user_id not in {row.user_id for row in state.users}:
        return state
    users = tuple(replace(row, ready=False) if row.user_id == user_id else row for row in state.users)
    subscriptions = tuple(replace(row, enabled=False) if row.user_id == user_id else row
                          for row in state.subscriptions)
    claims = tuple(replace(row, state="cancelled")
                   if row.user_id == user_id and row.state == "pending" else row for row in state.claims)
    return _sorted(replace(state, users=users, subscriptions=subscriptions, claims=claims))


def queue_candidate(state, listing_id, publication_at, max_jobs=MAX_JOBS):
    _validate(state)
    if not isinstance(listing_id, str) or not ID.fullmatch(listing_id) or not _time(publication_at):
        raise QueueStateError("invalid_candidate")
    if type(max_jobs) is not int or max_jobs <= 0 or max_jobs > MAX_ROWS:
        raise QueueStateError("invalid_limit")
    jobs = {row.listing_id: row for row in state.jobs}
    old = jobs.get(listing_id)
    if old is not None and publication_at <= old.publication_at:
        return state
    if old is None and len(jobs) >= max_jobs:
        return replace(state, job_overflow=state.job_overflow + 1)
    # A genuine later publication under an old ID may be evaluated again, but
    # durable per-user claims and per-epoch seen rows still prevent re-delivery.
    jobs[listing_id] = Job(listing_id, float(publication_at), "pending")
    return _sorted(replace(state, jobs=tuple(jobs.values())))


def begin_evaluation(state, listing_id, matching_search_ids):
    """Persist an in-flight snapshot; completion must recheck current epochs."""
    _validate(state)
    if not isinstance(matching_search_ids, (set, frozenset, tuple, list)):
        raise QueueStateError("invalid_matches")
    wanted = set(matching_search_ids)
    if any(not _positive(value) for value in wanted):
        raise QueueStateError("invalid_matches")
    jobs = {row.listing_id: row for row in state.jobs}
    job = jobs.get(listing_id)
    if job is None or job.state not in {"pending", "evaluating"}:
        raise QueueStateError("job_not_pending")
    users = {row.user_id: row for row in state.users}
    claims = {(row.user_id, row.listing_id) for row in state.claims}
    seen = {(row.search_id, row.listing_id, row.epoch): row for row in state.seen}
    targets = []
    for row in state.subscriptions:
        prior = seen.get((row.search_id, listing_id, row.epoch))
        if (row.search_id not in wanted or not row.enabled or not users[row.user_id].ready
                or row.started_at > job.publication_at or (row.user_id, listing_id) in claims
                or (prior is not None and prior.state != "pending")):
            continue
        if prior is None:
            seen[(row.search_id, listing_id, row.epoch)] = Seen(row.search_id, listing_id, row.epoch)
        targets.append((row.search_id, row.user_id, row.epoch))
    jobs[listing_id] = replace(job, state="evaluating")
    result = _sorted(replace(state, jobs=tuple(jobs.values()), seen=tuple(seen.values())))
    return result, EvaluationSnapshot(listing_id, job.publication_at, tuple(sorted(targets)))


def complete_evaluation(state, snapshot, confirmed_deal, max_claims=MAX_CLAIMS):
    """Recheck /stop, activation time and epoch after the shared evaluation."""
    _validate(state)
    if not isinstance(snapshot, EvaluationSnapshot) or type(confirmed_deal) is not bool:
        raise QueueStateError("invalid_evaluation")
    if type(max_claims) is not int or max_claims <= 0 or max_claims > MAX_ROWS:
        raise QueueStateError("invalid_limit")
    jobs = {row.listing_id: row for row in state.jobs}
    job = jobs.get(snapshot.listing_id)
    if job is None or job.publication_at != snapshot.publication_at or job.state != "evaluating":
        raise QueueStateError("evaluation_conflict")
    users = {row.user_id: row for row in state.users}
    subscriptions = {row.search_id: row for row in state.subscriptions}
    seen = {(row.search_id, row.listing_id, row.epoch): row for row in state.seen}
    claims = {(row.user_id, row.listing_id): row for row in state.claims}
    valid_by_user = {}
    final_seen = "checked" if confirmed_deal else "unvalued"
    for search_id, user_id, epoch in snapshot.targets:
        key = (search_id, snapshot.listing_id, epoch)
        if key in seen:
            seen[key] = replace(seen[key], state=final_seen)
        current = subscriptions.get(search_id)
        if (not confirmed_deal or current is None or current.user_id != user_id or current.epoch != epoch
                or not current.enabled or not users.get(user_id, UserState(user_id, False)).ready
                or current.started_at > snapshot.publication_at):
            continue
        valid_by_user.setdefault(user_id, []).append((search_id, epoch))
    overflow = state.claim_overflow
    deferred = False
    for user_id, matches in sorted(valid_by_user.items()):
        key = (user_id, snapshot.listing_id)
        if key in claims:
            continue
        if len(claims) >= max_claims:
            overflow += 1
            deferred = True
            for search_id, epoch in matches:
                seen_key = (search_id, snapshot.listing_id, epoch)
                seen[seen_key] = replace(seen[seen_key], state="pending")
            continue
        claims[key] = Claim(user_id, snapshot.listing_id, tuple(sorted(matches)))
    jobs[snapshot.listing_id] = replace(job, state="pending" if deferred else "complete" if confirmed_deal else "unvalued")
    result = _sorted(replace(state, jobs=tuple(jobs.values()), seen=tuple(seen.values()),
                             claims=tuple(claims.values()), claim_overflow=overflow))
    _validate(result)
    return result


def begin_send(state, user_id, listing_id):
    """Claim before hypothetical I/O and recheck a current matching epoch."""
    _validate(state)
    claims = {(row.user_id, row.listing_id): row for row in state.claims}
    claim = claims.get((user_id, listing_id))
    if claim is None or claim.state != "pending":
        raise QueueStateError("claim_not_pending")
    user = next((row for row in state.users if row.user_id == user_id), None)
    subscriptions = {row.search_id: row for row in state.subscriptions}
    valid = bool(user and user.ready and any(
        (current := subscriptions.get(search_id)) is not None and current.user_id == user_id
        and current.enabled and current.epoch == epoch for search_id, epoch in claim.matches))
    claims[(user_id, listing_id)] = replace(claim, state="sending" if valid else "cancelled")
    return _sorted(replace(state, claims=tuple(claims.values()))), valid


def finish_send(state, user_id, listing_id, outcome):
    """Model only an outcome token; no Telegram call exists in this module."""
    _validate(state)
    mapping = {"accepted": "sent", "ambiguous": "uncertain", "rejected": "failed", "rate_limited": "pending"}
    if outcome not in mapping:
        raise QueueStateError("invalid_send_outcome")
    claims = {(row.user_id, row.listing_id): row for row in state.claims}
    claim = claims.get((user_id, listing_id))
    if claim is None or claim.state != "sending":
        raise QueueStateError("claim_not_sending")
    claims[(user_id, listing_id)] = replace(claim, state=mapping[outcome])
    return _sorted(replace(state, claims=tuple(claims.values())))


def recover_after_restart(state):
    """Retry evaluations, never replay an ambiguous delivery attempt."""
    _validate(state)
    jobs = tuple(replace(row, state="pending") if row.state == "evaluating" else row for row in state.jobs)
    claims = tuple(replace(row, state="uncertain") if row.state == "sending" else row for row in state.claims)
    return _sorted(replace(state, jobs=jobs, claims=claims))
