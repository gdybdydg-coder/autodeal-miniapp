"""Prepared pricing policy; NOT a working AUTO.RIA range acquisition adapter.

`quote` is an internal contract for a future, verified provider adapter. These
field names are NOT claimed to exist in AUTO.RIA's documented AI API, which
currently documents avgPrice, not the listing UI's lower/upper boundaries.
Nothing in this module requests data or infers a lower bound from an average.
See docs/autoria-lower-bound-integration.md for the release blocker.
"""
import copy
import re
import time
from decimal import Decimal

from .valuation import FIELDS, is_deal, notification_condition_allowed, number, repair_notices

VERSION = "autoria-lower-bound-v1"
BASIS = "auto_ria_listing_market_range"
FACTOR = Decimal("0.95")
MAX_AGE = 300
EVIDENCE_FIELDS = (*FIELDS, "condition_exclusions")
QUOTE_FIELDS = {"source_id", "basis", "currency", "lower_usd", "upper_usd", "observed_at"}


def range_valid(quote, source_id, now):
    """Require an explicit range for this listing, never median/avgPrice/p25."""
    return bool(isinstance(quote, dict) and set(quote) == QUOTE_FIELDS
        and isinstance(source_id, str) and re.fullmatch(r"[1-9][0-9]{0,11}", source_id)
        and quote["source_id"] == source_id and quote["basis"] == BASIS
        and quote["currency"] == "USD"
        and number(quote["lower_usd"], positive=True)
        and number(quote["upper_usd"], positive=True)
        and quote["lower_usd"] <= quote["upper_usd"]
        and number(quote["observed_at"], positive=True)
        and -30 <= now - quote["observed_at"] <= MAX_AGE)


def estimate(candidate, quote, *, now=None):
    now = time.time() if now is None else now
    result = {"market": None, "discount": None, "comparables": 0,
              "valuation": "unavailable", "assessment": "unknown",
              "valuation_version": VERSION, "valuation_evidence": None,
              "valuation_reasons": ["provider_market_range_unavailable"]}
    if not isinstance(candidate, dict) or not range_valid(quote, candidate.get("id"), now):
        return result
    if (not notification_condition_allowed(candidate)
            or not number(candidate.get("price_usd"), positive=True)
            or not number(candidate.get("observed_at"), positive=True)
            or not -30 <= now - candidate["observed_at"] <= MAX_AGE):
        return {**result, "valuation_reasons": ["invalid_or_stale_candidate"]}
    # Retain full precision for saved-discount checks; only the card is rounded.
    market = Decimal(str(quote["lower_usd"])) * FACTOR
    price = Decimal(str(candidate["price_usd"]))
    evidence = {"version": VERSION, "basis": BASIS, "currency": "USD",
                "pricing_method": "provider_lower_bound_minus_5_percent",
                "adjustment_percent": 5, "evaluated_at": now,
                "source_range": copy.deepcopy(quote),
                "candidate": {key: copy.deepcopy(candidate.get(key)) for key in EVIDENCE_FIELDS},
                "condition_notices": repair_notices(candidate), "peers": []}
    return {**result, "market": float(market),
            "discount": float(((market - price) / market * 100).quantize(Decimal("0.1"))),
            "valuation": "provider_lower_bound_adjusted", "valuation_reasons": [],
            "assessment": "deal" if is_deal(candidate["price_usd"], float(market)) else "not_deal",
            "valuation_evidence": evidence}


def evidence_valid(car, now):
    proof = car.valuation_evidence
    if (not isinstance(proof, dict) or proof.get("version") != VERSION
            or proof.get("basis") != BASIS or proof.get("currency") != "USD"
            or proof.get("pricing_method") != "provider_lower_bound_minus_5_percent"
            or type(proof.get("adjustment_percent")) is not int or proof["adjustment_percent"] != 5
            or proof.get("peers") != [] or car.comparables != 0):
        return False
    candidate = proof.get("candidate")
    if not isinstance(candidate, dict) or any(candidate.get(key) != value for key, value in (
            ("id", car.source_id), ("price_usd", car.price), ("year", car.year),
            ("mileage", car.mileage), ("observed_at", car.observed_at))):
        return False
    rating = estimate(candidate, proof.get("source_range"), now=now)
    return (rating["valuation"] == "provider_lower_bound_adjusted"
            and rating["market"] == car.market
            and proof.get("condition_notices") == repair_notices(candidate))


def pricing_lines(car):
    """Only for a validated provider-range proof, not an asking-price proof."""
    if not evidence_valid(car, time.time()):
        raise ValueError("invalid_provider_range_evidence")

    def money(value):
        text = f"{Decimal(str(value)):,.2f}".rstrip("0").rstrip(".")
        return "$" + text.replace(",", " ").replace(".", ",")

    price, market = Decimal(str(car.price)), Decimal(str(car.market))
    percent = (market - price) / market * 100
    label = f"{abs(percent):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    difference = (f"📉 Нижче нашого орієнтира: {label}%" if percent >= 0
                  else f"📈 Вище нашого орієнтира: {label}%")
    return [f"📊 Наш ринковий орієнтир: ≈ {money(car.market)}",
            f"AUTO.RIA, нижня межа: {money(car.valuation_evidence['source_range']['lower_usd'])} − 5%",
            difference]
