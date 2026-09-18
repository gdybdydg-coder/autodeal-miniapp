"""Clearly labelled asking-price reference when an exact comparison is unavailable.

Only real, fresh peer observations are used. This is a broader comparison, not
an assertion that missing equipment/condition matches or that a sale is a bargain.
"""
import statistics
import time
from decimal import Decimal

from .valuation import FIELDS, MAX_AGE, estimate as exact_estimate, is_deal, number

VERSION = "reference-v1"
MIN_PEERS = 3
TARGET_PEERS = 5
YEAR_TOLERANCE = 2
MILEAGE_FRACTION = .4
MILEAGE_MINIMUM = 60000
OPTIONAL_DIMENSIONS = ("generation_id", "body_id", "fuel_id", "gear_id", "engine_cc")
EVIDENCE_FIELDS = (*FIELDS, "condition_exclusions")
VALUED = {"sample_median", "reference_median"}


def identifier(value):
    return type(value) is int and value > 0


def reasons(car, now, *, peer=False):
    result = []
    for key in ("brand_id", "model_id"):
        if not identifier(car.get(key)):
            result.append("missing_" + key)
    if not number(car.get("price_usd"), positive=True):
        result.append("invalid_price")
    if type(car.get("year")) is not int or not 1900 <= car["year"] <= 2100:
        result.append("invalid_year")
    if not number(car.get("observed_at"), positive=True) or not -30 <= now - car["observed_at"] <= MAX_AGE:
        result.append("stale_details")
    # Unknown condition on the candidate is disclosed, never interpreted as good.
    # Peer prices must come from explicitly eligible source-condition observations.
    if car.get("condition_exclusions") != [] or (peer and car.get("comparable_condition") is not True):
        result.append("unverified_condition")
    if car.get("mileage") is not None and not number(car["mileage"]):
        result.append("invalid_mileage")
    return result


def comparable(candidate, peer, now):
    rejected = reasons(peer, now, peer=True)
    for key in ("brand_id", "model_id", *OPTIONAL_DIMENSIONS):
        if identifier(candidate.get(key)) and peer.get(key) != candidate[key]:
            rejected.append(key)
    # Without explicit engine capacity the known modification is the only engine
    # anchor. Do not silently mix a base engine with a known performance variant.
    if not identifier(candidate.get("engine_cc")) and identifier(candidate.get("modification_id")):
        if peer.get("modification_id") != candidate["modification_id"]:
            rejected.append("modification_id")
    if type(peer.get("year")) is int and abs(peer["year"] - candidate["year"]) > YEAR_TOLERANCE:
        rejected.append("year")
    if number(candidate.get("mileage")):
        if (not number(peer.get("mileage")) or abs(peer["mileage"] - candidate["mileage"])
                > max(MILEAGE_MINIMUM, candidate["mileage"] * MILEAGE_FRACTION)):
            rejected.append("mileage")
    return list(dict.fromkeys(rejected))


def estimate(candidate, peers, *, now=None):
    now = time.time() if now is None else now
    blockers = reasons(candidate, now)
    evidence = {"version": VERSION, "basis": "asking_prices", "currency": "USD",
                "confidence": "indicative", "evaluated_at": now,
                "candidate": {key: candidate.get(key) for key in EVIDENCE_FIELDS},
                "peers": [], "rejected": [], "duplicate_entries": 0,
                "unknown_dimensions": [key for key in OPTIONAL_DIMENSIONS if not identifier(candidate.get(key))],
                "search": getattr(peers, "diagnostics", {})}
    result = {"market": None, "discount": None, "comparables": 0, "valuation": "insufficient_data",
              "valuation_reasons": blockers, "assessment": "unknown", "valuation_version": VERSION,
              "valuation_evidence": evidence}
    if blockers:
        return result
    unique = {}
    for peer in peers:
        sid = peer.get("id")
        if not sid or sid == candidate.get("id"):
            evidence["duplicate_entries"] += 1
            continue
        previous = unique.get(sid)
        if previous:
            evidence["duplicate_entries"] += 1
            if number(previous.get("observed_at")) and (not number(peer.get("observed_at"))
                    or previous["observed_at"] > peer["observed_at"]):
                continue
        unique[sid] = peer
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
    evidence["peers"] = [{key: peer.get(key) for key in EVIDENCE_FIELDS} for peer in accepted]
    prices = [peer["price_usd"] for peer in accepted]
    result["comparables"] = len(prices)
    if len(prices) < MIN_PEERS:
        return {**result, "valuation_reasons": ["insufficient_comparables"]}
    # Never cherry-pick cheap/expensive peers or discard a conflicting price to
    # manufacture a discount. A heterogeneous sample stays unpriced.
    if max(prices) / min(prices) > 2:
        return {**result, "valuation": "mixed_sample", "valuation_reasons": ["mixed_sample"]}
    market = float(statistics.median(Decimal(str(price)) for price in prices))
    evidence.update(median_usd=market, min_usd=min(prices), max_usd=max(prices),
                    oldest_peer_at=min(peer["observed_at"] for peer in accepted))
    return {**result, "market": market, "discount": round((1 - candidate["price_usd"] / market) * 100, 1),
            "valuation": "reference_median", "valuation_reasons": [],
            "assessment": "estimated_deal" if is_deal(candidate["price_usd"], market) else "estimated_not_deal"}


def notification_estimate(candidate, peers, *, now=None):
    exact = exact_estimate(candidate, peers, now=now)
    # A mixed exact sample must not become a bargain through a broader fallback.
    if exact["valuation"] != "insufficient_data":
        return exact
    reference = estimate(candidate, peers, now=now)
    return reference if reference["valuation"] in {"reference_median", "mixed_sample"} else exact
