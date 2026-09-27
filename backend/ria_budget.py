"""Operator-configured API caps; buying a package never resets local accounting."""
import os
from dataclasses import dataclass

TOTAL_OVERRIDE_ID = "auto-ria-total-cap-v1"


def total_cap(db, limits=None):
    """Durable owner-confirmed ceiling, without resetting any request counts.

    A later server total-cap edit supersedes the old snapshot. Hourly/daily
    edits do not silently withdraw a package the owner already confirmed.
    """
    from .models import SourceProbe
    limits = limits or BudgetLimits.env()
    row = db.get(SourceProbe, TOTAL_OVERRIDE_ID)
    data = row.result if row and row.status == "confirmed" else {}
    value = data.get("total_cap")
    if data.get("base_total_cap") == limits.total and type(value) is int and value >= 0:
        return value
    return limits.total


def peer_scan_limit():
    value = os.getenv("RIA_COMPARABLE_SCAN_LIMIT", "6")
    if not value.isascii() or not value.isdecimal() or not 6 <= int(value) <= 20:
        raise ValueError("AUTO.RIA comparable scan limit must be between 6 and 20")
    return int(value)


@dataclass(frozen=True)
class BudgetLimits:
    hourly: int = 24
    daily: int = 60
    total: int = 900

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (self.hourly, self.daily, self.total)):
            raise ValueError("AUTO.RIA budget caps must be positive integers")
        if not self.hourly <= self.daily <= self.total:
            raise ValueError("AUTO.RIA caps must satisfy hourly <= daily <= total")

    @classmethod
    def env(cls):
        names = ("RIA_REQUESTS_HOURLY_CAP", "RIA_REQUESTS_DAILY_CAP", "RIA_REQUESTS_TOTAL_CAP")
        values = [os.getenv(name) for name in names]
        if all(value is None for value in values):
            return cls()
        # A partial/invalid edit must not silently enlarge the remaining limits.
        if any(value is None or not value.isascii() or not value.isdecimal() for value in values):
            raise ValueError("Set all three AUTO.RIA request caps to positive integers")
        return cls(*(int(value) for value in values))

    def public(self):
        return {"hourly": self.hourly, "daily": self.daily, "total": self.total}
