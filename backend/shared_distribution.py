"""Local fan-out of one fresh candidate, with durable discovery boundaries."""
import math
import time

from sqlalchemy import select

from .auto_ria import RiaError
from .models import Delivery, Listing, MonitorSeen
from .ria_search import RiaSearch, matches

PROOF_FIELDS = ("publication_after", "shared_observed_at")


def proof(evidence):
    return {key: evidence[key] for key in PROOF_FIELDS if key in evidence}


def cutoff(settings, evidence):
    if not settings.ria_shared_distribution_enabled:
        return None
    if evidence.get("discovery_kind") == "active_window":
        if not (settings.ria_active_window_enabled and settings.ria_active_window_include_initial):
            return None
        value = evidence.get("shared_observed_at")
    else:
        # A local observation timestamp does not prove a new publication.
        value = evidence.get("publication_after")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return value if 0 < value <= time.time() else None


def claimed(db, uid, source_id):
    return db.scalar(select(Delivery.id).join(Listing, Listing.id == Delivery.listing_id)
        .where(Delivery.user_id == uid, Listing.source == "auto_ria",
               Listing.source_id == source_id).limit(1)) is not None


def targets(db, settings, source_id, evidence):
    from .monitor import EVIDENCE_SECONDS, active_members, source_filters

    boundary = cutoff(settings, evidence)
    candidate = evidence.get("candidate")
    if boundary is None or not candidate or not 0 <= time.time() - candidate["observed_at"] <= EVIDENCE_SECONDS:
        return []
    result, resolved_filters = [], evidence.setdefault("filters", {})
    failed = set()
    for search, watch, member in active_members(db):
        if member.started_at > boundary or db.get(MonitorSeen, (search.id, source_id)) is not None:
            continue
        filters = source_filters(search.filters)
        fingerprint = filters.fingerprint()
        if fingerprint in failed:
            continue
        if fingerprint not in resolved_filters:
            try:
                _, resolved_filters[fingerprint] = RiaSearch.cached_parameters(db, filters)
            except RiaError:
                # This group's ordinary poll still resolves and checks it.
                failed.add(fingerprint)
                continue
        if matches(candidate, filters, resolved_filters[fingerprint]) and not claimed(db, search.user_id, source_id):
            result.append((search.id, search.user_id, watch.epoch, search.filters))
    return result
