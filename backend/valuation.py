"""Auditable asking-price comparisons, independent of user budget and region."""
import hashlib
import json
import math
import re
import statistics
import time
from decimal import Decimal

VERSION = "asking-v5"
PRICE_ONLY_VERSION = "listing-price-v2"
INFORMATION_REASONS = {"incomplete_details", "insufficient_comparables", "mixed_sample",
                       "unverified_condition", "missing_valuation_details"}
MAX_AGE = 900
MIN_PEERS = 5
DIMENSIONS = ("brand_id", "model_id", "generation_id", "modification_id", "body_id", "fuel_id", "gear_id")
BASE_DIMENSIONS = tuple(key for key in DIMENSIONS if key != "modification_id")
FIELDS = ("id", "price_usd", "year", "mileage", "observed_at", "comparable_condition", "vehicle_key", "engine_cc", *DIMENSIONS)
PRICING_METHOD = {"pricing_method": "lower_quartile", "quantile": .25, "quantile_method": "linear"}


def price_statistics(prices):
    """Lower asking-price reference, not a prediction of a completed sale.

    Linear p25 uses h=(n-1)/4 on the sorted eligible observations. With five
    peers this is the second-lowest price, so one cheap outlier cannot set the
    reference. All accepted prices remain in the evidence; no fixed haircut,
    candidate price, user budget or desired discount changes the result.
    """
    ordered = sorted(Decimal(str(price)) for price in prices)
    position = Decimal(len(ordered) - 1) / 4
    lower = int(position)
    fraction = position - lower
    quartile = ordered[lower] + fraction * (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower])
    return {**PRICING_METHOD, "lower_quartile_usd": float(quartile),
            "median_usd": float(statistics.median(ordered)),
            "min_usd": float(ordered[0]), "max_usd": float(ordered[-1])}


def comparison_dimensions(car):
    # Prefer the exact modification. An explicitly stated engine capacity is a
    # separate comparison basis when the provider omits the modification ID.
    return DIMENSIONS if type(car.get("modification_id")) is int and car["modification_id"] > 0 else (
        (*BASE_DIMENSIONS, "engine_cc") if type(car.get("engine_cc")) is int and car["engine_cc"] > 0 else DIMENSIONS)


class PeerBatch(list):
    def __init__(self, values=(), **diagnostics):
        super().__init__(values)
        self.diagnostics = diagnostics


def number(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def is_deal(price, market, min_discount=15):
    # Classify using exact decimal inputs, never the rounded display percentage.
    return bool(number(price, positive=True) and number(market, positive=True)
                and number(min_discount) and min_discount <= 100
                and Decimal(str(price)) * 100 <= Decimal(str(market)) * (100 - Decimal(str(min_discount))))


def vehicle_key(vin):
    if not isinstance(vin, str):
        return None
    value = vin.strip().upper()
    if (not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value) or len(set(value)) < 4
            or re.search(r"X{3,}|0{6,}", value)):
        return None
    # No raw VIN is retained or exposed. Partial/masked values cannot merge cars.
    return hashlib.sha256(("auto-ria-vin:" + value).encode()).hexdigest()


def group_key(car):
    dimensions = (*BASE_DIMENSIONS, "engine_cc") if car.get("engine_cc") else DIMENSIONS
    values = [car.get(key) for key in dimensions]
    if any(type(value) is not int or value <= 0 for value in values):
        return ""
    return hashlib.sha256(json.dumps([dimensions, values]).encode()).hexdigest()


def reasons(car, now, dimensions=None):
    result = []
    if car.get("comparable_condition") is not True:
        result.append("unverified_condition")
    result.extend("missing_" + key for key in (dimensions or comparison_dimensions(car))
                  if type(car.get(key)) is not int or car[key] <= 0)
    if not number(car.get("price_usd"), positive=True):
        result.append("invalid_price")
    if type(car.get("year")) is not int or not 1900 <= car["year"] <= 2100:
        result.append("invalid_year")
    if not number(car.get("mileage")):
        result.append("invalid_mileage")
    if not number(car.get("observed_at"), positive=True) or not -30 <= now - car["observed_at"] <= MAX_AGE:
        result.append("stale_details")
    return result


def comparable(candidate, peer, now):
    dimensions = comparison_dimensions(candidate)
    # Some listings have a modification ID while otherwise equivalent peers do
    # not. Use the explicit engine size in that case; never accept a conflicting
    # known modification or guess an engine from seller text.
    if ("modification_id" in dimensions and not peer.get("modification_id")
            and type(candidate.get("engine_cc")) is int and candidate["engine_cc"] > 0):
        dimensions = (*BASE_DIMENSIONS, "engine_cc")
    rejected = reasons(peer, now, dimensions)
    rejected.extend(key for key in dimensions if peer.get(key) != candidate.get(key))
    # Even in engine comparisons, conflicting known modifications stay apart.
    if candidate.get("modification_id") and peer.get("modification_id") and candidate["modification_id"] != peer["modification_id"]:
        rejected.append("modification_id")
    if candidate.get("engine_cc") and peer.get("engine_cc") and candidate["engine_cc"] != peer["engine_cc"]:
        rejected.append("engine_cc")
    if type(peer.get("year")) is int and abs(peer["year"] - candidate["year"]) > 1:
        rejected.append("year")
    if number(peer.get("mileage")) and abs(peer["mileage"] - candidate["mileage"]) > max(30000, candidate["mileage"] * .2):
        rejected.append("mileage")
    return list(dict.fromkeys(rejected))


def estimate(candidate, peers, *, now=None):
    now = time.time() if now is None else now
    candidate_reasons = reasons(candidate, now)
    result = {"market": None, "discount": None, "comparables": 0, "valuation": "insufficient_data",
              "valuation_reasons": candidate_reasons, "assessment": "unknown", "valuation_version": VERSION}
    evidence = {"version": VERSION, "basis": "asking_prices", "currency": "USD", "threshold_percent": 15,
                **PRICING_METHOD,
                "comparison_basis": "engine_capacity" if "engine_cc" in comparison_dimensions(candidate) else "modification",
                "evaluated_at": now, "candidate": {key: candidate.get(key) for key in FIELDS},
                "peers": [], "rejected": [], "duplicate_entries": 0,
                "search": getattr(peers, "diagnostics", {})}
    result["valuation_evidence"] = evidence
    if candidate_reasons:
        return result
    # Freshest observation for repeated IDs, then one per known vehicle.
    unique = {}
    for peer in peers:
        source_id = peer.get("id")
        if not source_id or source_id == candidate["id"]:
            evidence["duplicate_entries"] += 1
            continue
        if source_id in unique:
            evidence["duplicate_entries"] += 1
            old_time, new_time = unique[source_id].get("observed_at", 0), peer.get("observed_at", 0)
            if number(old_time) and (not number(new_time) or old_time > new_time):
                continue
        unique[source_id] = peer
    fingerprints = {candidate["vehicle_key"]} if candidate.get("vehicle_key") else set()
    accepted = []
    for peer in unique.values():
        rejected = comparable(candidate, peer, now)
        fingerprint = peer.get("vehicle_key")
        if fingerprint and fingerprint in fingerprints:
            rejected.append("duplicate_vehicle")
        if rejected:
            evidence["rejected"].append({"id": peer["id"], "reasons": rejected})
            continue
        if fingerprint:
            fingerprints.add(fingerprint)
        accepted.append(peer)
    evidence["peers"] = [{key: peer.get(key) for key in FIELDS} for peer in accepted]
    if candidate.get("modification_id") and any(not peer.get("modification_id") for peer in accepted):
        evidence["comparison_basis"] = "modification_with_engine_fallback"
    prices = [peer["price_usd"] for peer in accepted]
    result["comparables"] = len(prices)
    if len(prices) < MIN_PEERS:
        why = ["insufficient_comparables"]
        if evidence["search"].get("limited"):
            why.append("comparison_limit")
        return {**result, "valuation_reasons": why}
    evidence.update(price_statistics(prices))
    market = evidence["lower_quartile_usd"]
    evidence.update(deal_threshold_usd=float(Decimal(str(market)) * Decimal("0.85")),
                    oldest_peer_at=min(peer["observed_at"] for peer in accepted))
    if max(prices) / min(prices) > 2:
        return {**result, "valuation": "mixed_sample", "valuation_reasons": ["mixed_sample"]}
    return {**result, "market": market, "discount": round((1 - candidate["price_usd"] / market) * 100, 1),
            "valuation": "sample_lower_quartile", "valuation_reasons": [],
            "assessment": "deal" if is_deal(candidate["price_usd"], market) else "not_deal"}


def evidence_valid(car, now):
    """Old or inconsistent valuation evidence must return to the monitor queue."""
    evidence = car.valuation_evidence
    from .reference_valuation import VERSION as REFERENCE_VERSION, estimate as reference_estimate
    if not evidence or evidence.get("version") not in {VERSION, REFERENCE_VERSION}:
        return False
    if any(evidence.get(key) != value for key, value in PRICING_METHOD.items()):
        return False
    reference = evidence.get("version") == REFERENCE_VERSION
    if reference and evidence.get("confidence") != "indicative":
        return False
    try:
        candidate = evidence.get("candidate", {})
        if any(candidate.get(key) != value for key, value in (
                ("id", car.source_id), ("price_usd", car.price), ("year", car.year),
                ("mileage", car.mileage), ("observed_at", car.observed_at))):
            return False
        result = (reference_estimate if reference else estimate)(candidate, evidence.get("peers", []), now=now)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
    # Valuation proof is shared; each subscription applies its own threshold.
    return (result["valuation"] == ("reference_lower_quartile" if reference else "sample_lower_quartile") and result["market"] == car.market
            and result["comparables"] == car.comparables)


def price_only_evidence(candidate, evaluated_at, uncertainty_reasons=None):
    """Proof for a clearly labelled informational (not verified-deal) alert.

    The price is still mandatory and freshly retrieved from the official detail
    endpoint. This proof never claims a market value or discount.
    """
    return {"version": PRICE_ONLY_VERSION, "basis": "fresh_listing_price",
            "evaluated_at": evaluated_at,
            "uncertainty_reasons": sorted(set(uncertainty_reasons or ["incomplete_details"])),
            "candidate": {key: candidate.get(key) for key in
                          ("id", "price_usd", "year", "mileage", "observed_at", "condition_exclusions")}}


def price_only_evidence_valid(car, now):
    evidence = car.valuation_evidence
    if not evidence or evidence.get("version") != PRICE_ONLY_VERSION or car.market is not None or car.comparables != 0:
        return False
    candidate = evidence.get("candidate")
    if not isinstance(candidate, dict):
        return False
    uncertainty = evidence.get("uncertainty_reasons")
    if (not isinstance(uncertainty, list) or not uncertainty
            or any(not isinstance(reason, str) or reason not in INFORMATION_REASONS for reason in uncertainty)
            or candidate.get("condition_exclusions") != []):
        return False
    return (candidate.get("id") == car.source_id
            and candidate.get("price_usd") == car.price
            and candidate.get("year") == car.year
            and candidate.get("mileage") == car.mileage
            and candidate.get("observed_at") == car.observed_at
            and number(car.price, positive=True)
            and number(car.observed_at, positive=True)
            and -30 <= now - car.observed_at <= 300)


def policy():
    return {"version": VERSION, "basis": "asking_prices", **PRICING_METHOD,
            "completed_sale_prices_available": False, "threshold_percent": 15,
            "threshold_scope": "subscription", "threshold_min": 0, "threshold_max": 100,
            "fractional_thresholds": True,
            "minimum_comparables": MIN_PEERS, "dimensions": list(DIMENSIONS),
            "missing_modification_fallback": "same_generation_body_fuel_gear_and_explicit_engine_capacity",
            "missing_modification_fallback_scope": "candidate_or_peer",
            "unknown_valuation_notification": "informational_without_market_or_discount",
            "condition_basis": "no_source_damage_parts_abroad_or_custom_flags",
            "year_tolerance": 1, "mileage_tolerance_percent": 20, "mileage_tolerance_min_km": 30000,
            "maximum_detail_age_seconds": MAX_AGE, "sample_max_price_ratio": 2,
            "user_price_and_region_affect_estimate": False,
            "reference_estimate": {"version": "reference-v2", "minimum_comparables": 3, **PRICING_METHOD,
                "confidence": "indicative", "known_attributes_must_match": True,
                "year_tolerance": 2, "mileage_tolerance_percent": 40, "mileage_tolerance_min_km": 60000,
                "subscription_threshold_applies": True, "comparison_request_cap": 8,
                "comparison_start_window_seconds": 8}}


def reason_category(rating):
    codes = rating.get("valuation_reasons", [])
    for code in ("stale_details", "comparison_limit", "mixed_sample"):
        if code in codes:
            return code
    if any(code.startswith(("missing_", "invalid_")) or code == "unverified_condition" for code in codes):
        return "missing_details"
    return "insufficient_comparables" if "insufficient_comparables" in codes else None
