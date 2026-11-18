"""Per-recipient publication boundary in the existing SourceProbe state store.

No observation, valuation refresh or updated-at timestamp proves publication.
An official dated search window can prove a lower bound when addDate is absent.
"""
import math
import time

from .models import SourceProbe


def key(user_id):
    return f"notification-baseline-{user_id}"


def boundary(db, user_id):
    row = db.get(SourceProbe, key(user_id), populate_existing=True)
    if row is None:
        return None
    value = row.result.get("baseline_at")
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_notification_baseline")
    return value


def after(baseline, *, published_at=None, publication_after=None, now=None):
    if baseline is None:
        return True  # No behavior change until an explicit reset.
    now = time.time() if now is None else now
    if published_at is not None:
        return (type(published_at) in (int, float) and math.isfinite(published_at)
                and baseline < published_at <= now)
    return (type(publication_after) in (int, float) and math.isfinite(publication_after)
            and 0 < baseline <= publication_after <= now)


def allowed(db, user_id, *, published_at=None, publication_after=None, now=None):
    return after(boundary(db, user_id), published_at=published_at,
                 publication_after=publication_after, now=now)


def listing_allowed(db, user_id, listing, now=None):
    pipeline = (listing.car.get("pipeline") or {}) if listing else {}
    return allowed(db, user_id, published_at=pipeline.get("publication_at", pipeline.get("source_added_at")),
                   publication_after=pipeline.get("publication_after"), now=now)


def publication_at(candidate):
    text = candidate.get("source_add_date_text")
    if text is not None:
        from .recent_publications import added_at
        value = added_at(text)  # Existing explicit Kyiv/DST source-clock policy.
        return value if value is not None else "unverified"
    return candidate.get("source_added_at")


def olx_published_at(car):
    dates = car.get("source_date_observations") or {}
    if dates.get("identity_matches") and not dates.get("issues"):
        return dates.get("values", {}).get("createdTime", {}).get("epoch")
    return None


def olx_allowed(engine, user_id, car):
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        return allowed(db, user_id, published_at=olx_published_at(car))
