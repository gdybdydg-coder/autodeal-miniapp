"""Coalesce identical discovery requests without changing subscription identity.

Optional characteristics are already omitted from AUTO.RIA discovery requests.
They remain part of each subscription's independent post-filter/valuation key.
Never combine different price, year, brand, model or region constraints.
"""
from collections import defaultdict

from sqlalchemy import select

from .models import Filters, MonitorActiveWindow, MonitorFeed, User


def discovery_filters(raw):
    data = Filters.model_validate(raw).model_dump(by_alias=True)
    data.update(body=[], fuel=[], transmission=[], mileage={}, onlyDeals=True, minDiscount=15)
    return Filters.model_validate(data)


def merge(monitor, db):
    from .monitor import active_members

    members = active_members(db)
    candidates = defaultdict(set)
    for search, _, member in members:
        candidates[discovery_filters(search.filters).fingerprint()].add(member.feed_id)
    keys = {key for key, ids in candidates.items() if len(ids) > 1}
    if not keys:
        return
    # Subscription edits and /stop lock the user first. Re-read memberships
    # under these locks so a concurrent edit cannot be overwritten by regrouping.
    users = sorted({search.user_id for search, _, _ in members
                    if discovery_filters(search.filters).fingerprint() in keys})
    for uid in users:
        db.scalar(select(User).where(User.id == uid).with_for_update()
                  .execution_options(populate_existing=True))
    if not monitor.owned(db):
        return
    db.expire_all()
    grouped = defaultdict(list)
    for search, watch, member in active_members(db):
        key = discovery_filters(search.filters).fingerprint()
        if key in keys and search.user_id in users:
            grouped[key].append((search, watch, member))
    for key, rows in grouped.items():
        ids = sorted({member.feed_id for _, _, member in rows})
        if len(ids) < 2:
            continue
        feeds = [db.get(MonitorFeed, feed_id) for feed_id in ids]
        # Finish every frozen paginated window before moving its recipients.
        # Retain error/quota backoffs; a merge must never bypass a retry gate.
        if any(not feed or feed.context or feed.status not in {"starting", "watching"}
               or discovery_filters(feed.filters).fingerprint() != key for feed in feeds):
            continue
        windows = [db.get(MonitorActiveWindow, feed_id) for feed_id in ids]
        if monitor.settings.ria_active_window_enabled:
            # Identical snapshots preserve the meaning of 'new to the page',
            # including subscriptions whose initial page was only a baseline.
            if any(not row for row in windows):
                continue
            if (len({frozenset(row.window or []) for row in windows}) != 1
                    or len({row.checked_at > 0 for row in windows}) != 1
                    or any(row.status not in {"starting", "watching", "baseline"} for row in windows)):
                continue
        target = feeds[0]
        target.filters = discovery_filters(target.filters).canonical()
        # Replay only the normal overlap from the earliest committed cursor.
        # Existing per-search seen records suppress already processed IDs.
        target.cursor = min(feed.cursor for feed in feeds)
        target.started_at = min(feed.started_at for feed in feeds)
        target.next_poll = min(feed.next_poll for feed in feeds)
        target.checked_at = min(feed.checked_at for feed in feeds)
        if monitor.settings.ria_active_window_enabled:
            windows[0].next_poll = min(row.next_poll for row in windows)
            windows[0].checked_at = min(row.checked_at for row in windows)
        for _, watch, member in rows:
            member.feed_id = target.id
            watch.next_poll = target.next_poll
        # Keep dormant source rows for diagnostics. Epochs, interests, matches,
        # jobs, delivery claims and activation times are deliberately untouched.
