"""Kyiv wall-clock polling targets, bounded by the existing operator caps."""
import math
import os
import time
from datetime import datetime
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE = "Europe/Kyiv"
try:
    KYIV = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError:
    # Missing system tzdata must not stop discovery or guess a UTC offset.
    KYIV = None

PERIODS = (("night", 23, 8, 3600), ("day", 8, 18, 110), ("evening", 18, 23, 60))


def fixed_interval():
    """Optional owner-set all-day target; all durable quota gates still apply."""
    value = os.getenv("RIA_POLL_INTERVAL_SECONDS")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal() or not 30 <= int(value) <= 3600:
        raise ValueError("RIA_POLL_INTERVAL_SECONDS must be an integer from 30 to 3600")
    return int(value)


def policy(groups, limits, now=None):
    fixed = fixed_interval()
    if KYIV is None:
        return {"enabled": False, "timezone": TIMEZONE, "reason": "timezone_unavailable"}
    now = time.time() if now is None else now
    hour = datetime.fromtimestamp(now, KYIV).hour
    groups = max(0, groups)
    hourly = max(1, limits.hourly * 3 // 4)
    daily = max(1, limits.daily * 3 // 4)
    targets = tuple((name, start, end, fixed if fixed is not None else target)
                    for name, start, end, target in PERIODS)
    requested_calls = sum(Fraction(groups * ((end - start) % 24) * 3600, target)
                          for _, start, end, target in targets)
    scale = max(Fraction(1), requested_calls / daily)
    periods = []
    for name, start, end, target in targets:
        interval = max(math.ceil(target * scale), math.ceil(Fraction(groups * 3600, hourly)))
        periods.append({"name": name, "start": f"{start:02}:00", "end": f"{end:02}:00",
                        "requested_interval_seconds": target, "interval_seconds": interval})
    active = periods[1 if 8 <= hour < 18 else 2 if 18 <= hour < 23 else 0]
    return {"enabled": True, "timezone": TIMEZONE, "active_period": active["name"],
            "mode": "fixed_interval" if fixed is not None else "kyiv_schedule",
            "requested_interval_seconds": active["requested_interval_seconds"],
            "interval_seconds": active["interval_seconds"],
            "budget_limited": active["interval_seconds"] > active["requested_interval_seconds"],
            "periods": periods,
            # Normal 24-hour day, one page/poll. Extra pages, details, quotes,
            # retries and DST days still pass the durable rolling quota gate.
            "requested_search_calls_per_day": math.ceil(requested_calls),
            "minimum_planning_limits": {
                "hourly": math.ceil(Fraction(groups * 3600 * 4, min(p[3] for p in targets) * 3)),
                "daily": math.ceil(requested_calls * 4 / 3)},
            "estimate_basis": "one_page_per_poll_standard_24h_day"}
