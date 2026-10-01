"""Fail-closed filter gate for the isolated zero-paid-API experiment.

The module has no network, backend, database or delivery imports.  A future
source adapter may pass only normalized, public evidence into ``gate``.  The
result can authorize valuation research, never Telegram delivery.
"""

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import FrozenSet, Optional, Tuple


LISTING_ID = re.compile(r"[1-9][0-9]{0,11}")
PASSENGER = "passenger"


@dataclass(frozen=True)
class Span:
    minimum: Optional[Decimal] = None
    maximum: Optional[Decimal] = None


@dataclass(frozen=True)
class SearchFilter:
    regions: FrozenSet[str] = frozenset()
    price_usd: Span = Span()
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    bodies: FrozenSet[str] = frozenset()
    fuels: FrozenSet[str] = frozenset()
    transmissions: FrozenSet[str] = frozenset()
    mileage_min_km: Optional[int] = None
    mileage_max_km: Optional[int] = None
    min_discount_percent: Decimal = Decimal("0")


@dataclass(frozen=True)
class PublicEvidence:
    listing_id: str
    publication_proven: bool
    active: Optional[bool]
    category: Optional[str]
    price_usd: Optional[Decimal]
    region: Optional[str] = None
    year: Optional[int] = None
    body: Optional[str] = None
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    mileage_km: Optional[int] = None
    abroad: Optional[bool] = None
    needs_customs: Optional[bool] = None
    damaged: Optional[bool] = None
    repair_parts: Optional[bool] = None


@dataclass(frozen=True)
class GateResult:
    eligible_for_valuation: bool
    ready_for_delivery: bool
    blockers: Tuple[str, ...]
    notices: Tuple[str, ...]


def _outside(value, minimum, maximum):
    return ((minimum is not None and value < minimum)
            or (maximum is not None and value > maximum))


def _known_set_conflict(value, accepted):
    # Missing optional details are unknown, not contradictions.
    return value is not None and bool(accepted) and value not in accepted


def gate(evidence: PublicEvidence, filters: SearchFilter) -> GateResult:
    """Apply production-compatible safety semantics to normalized evidence.

    Passenger category, a genuinely new publication, active availability and
    a positive current USD price are mandatory. Region is mandatory only when
    the user selected regions. Known optional characteristics must match;
    unknown optional characteristics do not hide an otherwise eligible car.
    Known abroad/customs exclusions block, while damage/repair markers are
    notices only. No result from this experimental gate is delivery-ready.
    """
    blockers = []
    notices = []

    if not isinstance(evidence.listing_id, str) or not LISTING_ID.fullmatch(evidence.listing_id):
        blockers.append("invalid_listing_id")
    if evidence.publication_proven is not True:
        blockers.append("publication_not_proven")
    if evidence.active is not True:
        blockers.append("active_status_not_proven" if evidence.active is None else "inactive")
    if evidence.category != PASSENGER:
        blockers.append("category_not_proven" if evidence.category is None else "not_passenger")

    price = evidence.price_usd
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        blockers.append("positive_current_price_not_proven")
    elif _outside(price, filters.price_usd.minimum, filters.price_usd.maximum):
        blockers.append("price_mismatch")

    if filters.regions:
        if evidence.region is None:
            blockers.append("region_not_proven")
        elif evidence.region not in filters.regions:
            blockers.append("region_mismatch")

    if evidence.year is not None and _outside(evidence.year, filters.year_min, filters.year_max):
        blockers.append("year_mismatch")
    if _known_set_conflict(evidence.body, filters.bodies):
        blockers.append("body_mismatch")
    if _known_set_conflict(evidence.fuel, filters.fuels):
        blockers.append("fuel_mismatch")
    if _known_set_conflict(evidence.transmission, filters.transmissions):
        blockers.append("transmission_mismatch")
    if evidence.mileage_km is not None and _outside(
            evidence.mileage_km, filters.mileage_min_km, filters.mileage_max_km):
        blockers.append("mileage_mismatch")

    if evidence.abroad is True:
        blockers.append("abroad")
    elif evidence.abroad is None:
        notices.append("abroad_unknown")
    if evidence.needs_customs is True:
        blockers.append("needs_customs")
    elif evidence.needs_customs is None:
        notices.append("customs_unknown")

    if evidence.damaged is True:
        notices.append("damage")
    if evidence.repair_parts is True:
        notices.append("repair_parts")

    # confirmed_deals_only and minDiscount require a separate validated market
    # estimate.  This stage deliberately has no market-price input.
    if not blockers:
        notices.append("valuation_required")
    return GateResult(not blockers, False, tuple(blockers), tuple(notices))
