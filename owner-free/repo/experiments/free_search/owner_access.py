"""Read-only bridge to CURRENT owner access; never selects another recipient.

Not imported by the running application. No activation, DB writes or sends.
Owner role is not a paid-access exception. Re-read before each future stage.
"""
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend import billing, paid_source_access
from backend.models import Delivery, Filters, Listing, MonitorWatch, Search, User


@dataclass(frozen=True)
class OwnerSearch:
    search_id: int
    fingerprint: str
    epoch: str
    filters: dict


def current_searches(engine, settings, now):
    if not settings.live or not billing.verified_admin(settings):
        return (), 'owner_not_verified'
    if not paid_source_access.strict(engine):
        return (), 'strict_paid_policy_required'
    with Session(engine) as db:
        owner = db.get(User, settings.admin_telegram_id)
        if not owner or not owner.ready:
            return (), 'owner_stopped'
        if not paid_source_access.allowed(db, owner.id, now):
            return (), 'owner_confirmed_payment_required'
        rows = db.execute(select(Search, MonitorWatch).join(
            MonitorWatch, MonitorWatch.search_id == Search.id).where(
            Search.user_id == owner.id, Search.enabled.is_(True))).all()
        result = []
        for search, watch in rows:
            filters = Filters.model_validate(search.filters)
            if filters.fingerprint() != search.fingerprint or not watch.epoch:
                return (), 'owner_search_snapshot_inconsistent'
            result.append(OwnerSearch(search.id, search.fingerprint,
                                      watch.epoch, filters.canonical()))
        return tuple(result), 'eligible' if result else 'no_enabled_owner_search'


def revalidate(engine, settings, snapshot, listing_id, now, *, comparison=False):
    """Current /stop, expiry, search edits, epoch and ordinary RIA dedup.

    This is a read-only preview. A sender MUST eventually perform its own
    transactional claim; this function alone cannot authorize transport.
    """
    searches, reason = current_searches(engine, settings, now)
    if reason != 'eligible':
        return reason
    if snapshot not in searches:
        return 'owner_search_changed'
    with Session(engine) as db:
        prior = db.execute(select(Delivery.id,Listing.car).join(Listing,
            Listing.id == Delivery.listing_id).where(
            Delivery.user_id == settings.admin_telegram_id,
            Listing.source == 'auto_ria', Listing.source_id == listing_id)).first()
        if prior is not None:
            if comparison is True:
                pipeline=(prior[1] or {}).get('pipeline') or {}
                if pipeline.get('discovery_kind')!='owner_public_first_observed':
                    return 'eligible'  # Only the separate comparison sender may use this.
            # No resetting sent, uncertain, pending or old failed attempts.
            return 'existing_owner_delivery'
    return 'eligible'
