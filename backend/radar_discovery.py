"""Lossless unions of Radar discovery rectangles on the existing feed scheduler.

Only one dimension is widened at a time while all other dimensions are equal.
That rule avoids a cross-product of unrelated filters and keeps local matching
authoritative. Frozen pages/backoffs must finish before a group changes.
An explicitly requested all-brand branch can absorb brand/model selections
only when every other dimension is equal.
"""
import hashlib
import json
import math
from collections import defaultdict

from sqlalchemy import select

from .models import Filters, MonitorFeed, Range, User


def _range(span):
    return (span.from_ if span.from_ is not None else 0,
            span.to if span.to is not None else math.inf)


def _key(filters, omit):
    data = filters.canonical()
    for name in omit:
        data.pop(name, None)
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def plan(rows):
    """Return deterministic, lossless compatible groups with member rows."""
    from .compatible_searches import discovery_filters
    grouped = {}
    for row in rows:
        filters = discovery_filters(row[0].filters)
        key = filters.fingerprint()
        grouped.setdefault(key, {"filters": filters, "rows": []})["rows"].append(row)
    groups = list(grouped.values())
    # Repeating bounded passes allows a price merge to expose a year merge and
    # vice versa. Every successful merge reduces the number of groups.
    while True:
        before = len(groups)
        for dimension in ("price", "year", "region", "model", "brand"):
            partitions = defaultdict(list)
            for group in groups:
                omitted = ("brand", "model") if dimension == "brand" else (dimension,)
                partitions[_key(group["filters"], omitted)].append(group)
            next_groups = []
            for partition in partitions.values():
                if dimension in ("price", "year"):
                    ordered = sorted(partition, key=lambda g: _range(getattr(g["filters"], dimension)))
                    current = ordered[0]
                    for item in ordered[1:]:
                        low, high = _range(getattr(current["filters"], dimension))
                        other_low, other_high = _range(getattr(item["filters"], dimension))
                        adjacent = (high + 1 if dimension == "year" else math.nextafter(high, math.inf))
                        if other_low <= adjacent:
                            upper = max(high, other_high)
                            span = Range.model_validate({"from": min(low, other_low),
                                "to": None if math.isinf(upper) else upper})
                            current["filters"] = current["filters"].model_copy(update={dimension: span})
                            current["rows"].extend(item["rows"])
                        else:
                            next_groups.append(current)
                            current = item
                    next_groups.append(current)
                elif dimension == "region":
                    regions = [g["filters"].regions for g in partition]
                    union = [] if any(not r for r in regions) else sorted({r for values in regions for r in values})
                    # Existing source adapter supports a bounded states[] list.
                    if len(union) > 30:
                        next_groups.extend(partition)
                    else:
                        next_groups.append({"filters": partition[0]["filters"].model_copy(update={"region": union}),
                                            "rows": [r for g in partition for r in g["rows"]]})
                elif dimension == "model":
                    # Without a requested whole-brand branch, retain one query
                    # per model. Never broaden A+B to every model of a brand.
                    wildcard = next((g for g in partition if not g["filters"].model), None)
                    if wildcard:
                        next_groups.append({"filters": wildcard["filters"],
                                            "rows": [r for g in partition for r in g["rows"]]})
                    else:
                        next_groups.extend(partition)
                else:
                    # A requested all-brand radar already covers every model.
                    # Reuse that exact price/year/region query for specifics;
                    # never create a wildcard from selected brands alone.
                    wildcard = next((g for g in partition if not g["filters"].brand
                                     and not g["filters"].model), None)
                    if wildcard:
                        next_groups.append({"filters": wildcard["filters"],
                                            "rows": [r for g in partition for r in g["rows"]]})
                    else:
                        next_groups.extend(partition)
            groups = next_groups
        if len(groups) == before:
            return sorted(groups, key=lambda g: g["filters"].fingerprint())


def merge(monitor, db):
    from .monitor import active_members
    members = active_members(db)
    if not members:
        return
    desired = plan(members)
    def group_key(group):
        return hashlib.sha256(("radar-discovery-v1:" + group["filters"].fingerprint()).encode()).hexdigest()
    if all({m.feed_id for _, _, m in g["rows"]} == {group_key(g)} for g in desired):
        return  # Stable plans do not lock users or write any feed state.
    # Same ascending owner lock order as editing, pausing and delivery.
    for uid in sorted({s.user_id for s, _, _ in members}):
        db.scalar(select(User).where(User.id == uid).with_for_update().execution_options(populate_existing=True))
    if not monitor.owned(db):
        return
    db.expire_all()
    members = active_members(db)
    source_feeds = {key: db.get(MonitorFeed, key) for key in {m.feed_id for _, _, m in members}}
    for group in plan(members):
        rows, filters = group["rows"], group["filters"]
        old_ids = {m.feed_id for _, _, m in rows}
        feeds = [source_feeds[key] for key in old_ids]
        # No cursor reset, retry bypass or in-flight page recipient changes.
        if any(not feed or feed.context or feed.status not in {"starting", "watching"} for feed in feeds):
            continue
        key = hashlib.sha256(("radar-discovery-v1:" + filters.fingerprint()).encode()).hexdigest()
        if old_ids == {key} and source_feeds[key].filters == filters.canonical():
            continue
        target = db.get(MonitorFeed, key)
        if target is not None and key in source_feeds and (target.context or target.status not in {"starting", "watching"}):
            continue
        starts = {ident: min(m.started_at for _, _, m in rows if m.feed_id == ident) for ident in old_ids}
        # A new activation can reuse a dormant canonical feed from months ago.
        # It owns no coverage before its current members' activation boundary;
        # do not rewind another user's healthy shared feed to that stale cursor.
        cursor = min(max(feed.cursor, starts[feed.id]) for feed in feeds)
        started = min(max(feed.started_at, starts[feed.id]) for feed in feeds)
        next_poll = min(feed.next_poll for feed in feeds)
        checked = min(feed.checked_at for feed in feeds)
        if target is None:
            target = MonitorFeed(id=key, filters=filters.canonical(), started_at=started, cursor=cursor)
            db.add(target)
        else:
            target.filters = filters.canonical()
            target.started_at, target.cursor = started, cursor
        target.context, target.status = {}, "watching"
        target.next_poll, target.checked_at = next_poll, checked
        for _, watch, member in rows:
            member.feed_id, watch.next_poll = key, next_poll
        db.flush()
