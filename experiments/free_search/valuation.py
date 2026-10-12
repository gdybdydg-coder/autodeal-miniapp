"""Experimental, offline valuation using comparable *asking* prices.

This is not AUTO.RIA's appraisal, a sale-price model or production approval.
It has no IO or backend imports. Caller-supplied observations are evaluated
strictly as of a timestamp; it never downloads/caches a paid appraisal.
Unknowns carry stable reasons for a caller's durable retry/review queue.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping
import math
import re


PROVENANCE = "experimental_peer_asking_v1"
D = Decimal
DAY = 86400


@dataclass(frozen=True)
class ValuationPolicy:
    min_peers: int = 5
    max_peer_age_seconds: int = 30 * DAY
    max_subject_price_age_seconds: int = DAY
    max_year_gap: int = 1
    mileage_absolute_gap_km: int = 50000
    mileage_relative_gap: Decimal = D("0.35")
    # Asking-price lower quartile, deliberately not the production RIA -5% rule.
    reference_quantile: Decimal = D("0.25")
    maximum_relative_spread: Decimal = D("0.60")
    suspicious_price_ratio: Decimal = D("0.15")
    max_peers: int = 10000

    def __post_init__(self):
        ints = (self.min_peers, self.max_peer_age_seconds,
                self.max_subject_price_age_seconds, self.max_year_gap,
                self.mileage_absolute_gap_km, self.max_peers)
        if any(type(n) is not int or n < 0 for n in ints):
            raise ValueError("invalid_valuation_policy")
        if not 3 <= self.min_peers <= self.max_peers <= 100000:
            raise ValueError("invalid_valuation_policy")
        if self.max_peer_age_seconds <= 0 or self.max_subject_price_age_seconds <= 0:
            raise ValueError("invalid_valuation_policy")
        for value in (self.mileage_relative_gap, self.reference_quantile,
                      self.maximum_relative_spread, self.suspicious_price_ratio):
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 < value < 1:
                raise ValueError("invalid_valuation_policy")


@dataclass(frozen=True)
class ValuationOutcome:
    status: str
    classification: str
    reason: str
    currency: str | None
    reference_price: Decimal | None
    discount_percent: Decimal | None
    sample_count: int
    as_of: float
    lower_asking_price: Decimal | None = None
    median_asking_price: Decimal | None = None
    upper_asking_price: Decimal | None = None
    peer_age_min_seconds: float | None = None
    peer_age_max_seconds: float | None = None
    input_count: int = 0
    excluded: tuple[tuple[str, int], ...] = ()
    notices: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    region_sample_count: int = 0
    provenance: str = PROVENANCE
    production_approved: bool = False
    # A fitted/calibrated confidence score would falsely imply measured quality.
    confidence: str = "uncalibrated"

    @property
    def reference_price_usd(self):
        return self.reference_price if self.currency == "USD" else None

    def as_dict(self):
        return {
            "status": self.status, "classification": self.classification,
            "reason": self.reason, "currency": self.currency,
            "reference_price": _json_number(self.reference_price),
            "reference_price_usd": _json_number(self.reference_price_usd),
            "discount_percent": _json_number(self.discount_percent),
            "sample_count": self.sample_count, "as_of": self.as_of,
            "lower_asking_price": _json_number(self.lower_asking_price),
            "median_asking_price": _json_number(self.median_asking_price),
            "upper_asking_price": _json_number(self.upper_asking_price),
            "peer_age_min_seconds": self.peer_age_min_seconds,
            "peer_age_max_seconds": self.peer_age_max_seconds,
            "input_count": self.input_count, "excluded": dict(self.excluded),
            "notices": list(self.notices), "missing_fields": list(self.missing_fields),
            "region_sample_count": self.region_sample_count,
            "provenance": self.provenance, "production_approved": False,
            "confidence": self.confidence,
        }


def _json_number(value):
    return None if value is None else str(value)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and abs(number) <= D("1e12") else None


def _time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _word(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    return " ".join(value.casefold().split())


def _engine(row):
    cc = _number(row.get("engine_cc"))
    if cc is None:
        liters = _number(row.get("engine_liters"))
        cc = None if liters is None else liters * 1000
    return cc if cc is not None and 0 < cc <= 20000 else None


def _price(row):
    value = row.get("price")
    if value is None:
        return _number(row.get("price_usd")), "USD" if row.get("price_usd") is not None else None
    currency = row.get("currency")
    return _number(value), currency if currency in {"USD", "UAH", "EUR"} else None


# These are explicit price qualifiers, not generic words about damaged cars.
# No claim is made that this finite multilingual vocabulary detects every trick.
UPFRONT = re.compile(r"\b(?:перш(?:ий|ого)\s+внес(?:ок|ку)|початков(?:ий|ого)\s+внес(?:ок|ку)|"
                     r"перв(?:ый|ого)\s+взнос(?:а)?|аванс|down\s*payment|upfront)\b", re.I)
INSTALLMENT = re.compile(r"(?:\b(?:на\s+місяць|в\s+месяц|щомісячн\w*|ежемесячн\w*|monthly|installment)\b|/\s*(?:міс|мес|month)\b)", re.I)
PART = re.compile(r"\b(?:ціна\s+(?:за\s+)?детал[іь]|цена\s+(?:за\s+)?детал[ьи]|"
                  r"price\s+(?:for\s+)?(?:a\s+)?part|окрем(?:а|ої)\s+детал[іь]|"
                  r"(?:ціна|цена)\s+за\s+(?:двигун|мотор|двері|дверь|фару|колесо))\b", re.I)
PLACEHOLDER = re.compile(r"\b(?:ціна\s+умовна|цена\s+условная|placeholder|договірна\s+ціна|ціна\s+договірна)\b", re.I)


def price_flags(row: Mapping) -> tuple[str, ...]:
    """Price ambiguity flags only; damage/whole-car-for-parts is never a ban."""
    flags = set()
    kind = _word(row.get("price_kind"))
    if kind in {"upfront", "installment", "part", "placeholder"}:
        flags.add("price_" + kind)
    if kind is not None and kind not in {"whole_vehicle", "full", "upfront", "installment", "part", "placeholder"}:
        flags.add("price_kind_unknown")
    # Explicit qualifiers are higher priority than an optimistic upstream flag.
    context = " ".join(str(row.get(key) or "")[:2000]
                       for key in ("title", "price_context"))
    if UPFRONT.search(context):
        flags.add("price_upfront")
    if INSTALLMENT.search(context):
        flags.add("price_installment")
    if PART.search(context):
        flags.add("price_part")
    if PLACEHOLDER.search(context):
        flags.add("price_placeholder")
    if row.get("whole_vehicle") is False:
        flags.add("not_whole_vehicle")
    # "На запчастини" alone can mean an entire damaged car, so is not a flag.
    # Extremely tiny prices are uncertain even when entered as a numeric offer.
    price, currency = _price(row)
    floor = {"USD": D("100"), "EUR": D("100"), "UAH": D("1000")}.get(currency)
    if price is not None and price > 0 and floor is not None and price < floor:
        flags.add("price_placeholder_range")
    return tuple(sorted(flags))


def _row_meta(row, as_of, max_age):
    observed = _time(row.get("observed_at"))
    price_at = _time(row.get("price_observed_at", row.get("observed_at")))
    if observed is None or price_at is None:
        return "observation_time_unknown", None
    if observed > as_of or price_at > as_of or price_at > observed:
        return "future_observation", None
    published_raw = row.get("publication_at", row.get("published_at"))
    if published_raw is not None:
        published = _time(published_raw)
        if published is None or published > as_of or published > observed:
            return "publication_time_conflict", None
    if as_of - price_at > max_age:
        return "stale_price", None
    return None, (observed, price_at)


def _year(value):
    n = _number(value)
    return int(n) if n is not None and n == n.to_integral_value() and 1900 <= n <= 2100 else None


def _mismatch(subject, peer, policy):
    for field in ("brand", "model"):
        if _word(peer.get(field)) != _word(subject.get(field)):
            return field + "_mismatch"
    sy, py = _year(subject.get("year")), _year(peer.get("year"))
    if py is None or abs(sy - py) > policy.max_year_gap:
        return "year_mismatch_or_unknown"
    for field in ("generation", "transmission", "fuel", "body"):
        left, right = _word(subject.get(field)), _word(peer.get(field))
        if left is not None and right is not None and left != right:
            return field + "_mismatch"
    se, pe = _engine(subject), _engine(peer)
    if se is not None and pe is not None and abs(se - pe) > max(D("100"), se * D("0.10")):
        return "engine_mismatch"
    sm, pm = _number(subject.get("mileage_km")), _number(peer.get("mileage_km"))
    if sm is not None and pm is not None:
        if sm < 0 or pm < 0:
            return "invalid_mileage"
        if abs(sm - pm) > max(D(policy.mileage_absolute_gap_km), sm * policy.mileage_relative_gap):
            return "mileage_mismatch"
    return None


def _quantile(values, fraction):
    """Deterministic linearly interpolated quantile; no fitted coefficients."""
    ordered = sorted(values)
    pos = D(len(ordered) - 1) * fraction
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def _valuation_snapshot(row):
    # Ignore labels, paid appraisal fields and arbitrary annotations completely.
    keys = ("brand", "model", "year", "generation", "transmission", "fuel", "body",
            "engine_cc", "engine_liters", "mileage_km", "region", "price", "price_usd",
            "currency", "price_observed_at", "publication_at", "published_at",
            "duplicate_key", "title", "price_context", "price_kind", "whole_vehicle")
    return tuple((key, row.get(key)) for key in keys)


def _missing(row):
    fields = [key for key in ("generation", "transmission", "fuel", "region")
              if _word(row.get(key)) is None]
    if _engine(row) is None:
        fields.append("engine")
    if _number(row.get("mileage_km")) is None:
        fields.append("mileage_km")
    return tuple(sorted(fields))


def estimate(details: Mapping, peers: Iterable[Mapping], as_of,
             policy: ValuationPolicy = ValuationPolicy()) -> ValuationOutcome:
    """Estimate a lower asking-price quartile from point-in-time peers.

    Core identity/year/current amount are required; absent fuel/gear/engine etc.
    are reported, not automatic rejection. Known incompatibilities are rejected.
    Prices use one exact currency, with no invented exchange rates. Latest known
    snapshot per ID and caller-evidenced duplicate group count only once. Peers
    beyond the bounded input budget cause unknown, never silent partial success.
    Per-user minimum discount is applied separately by the pipeline.
    """
    now = _time(as_of)
    if now is None:
        raise ValueError("invalid_as_of")
    if not isinstance(details, Mapping):
        raise ValueError("invalid_details")
    if not isinstance(policy, ValuationPolicy):
        raise ValueError("invalid_valuation_policy")
    price, currency = _price(details)
    missing = _missing(details)
    counts, notices = Counter(), {"asking_prices_not_sales", "quality_not_live_validated"}
    if missing:
        notices.add("subject_optional_fields_missing")
    if details.get("damaged") is True or details.get("repair_parts") is True:
        notices.add("whole_car_condition_not_excluded")
    state = dict(currency=currency, reference_price=None, discount_percent=None,
                 sample_count=0, as_of=now, input_count=0, missing_fields=missing)

    def outcome(reason, classification="insufficient_data", **extra):
        state.update(extra)
        return ValuationOutcome(status="unknown", classification=classification,
                                reason=reason, excluded=tuple(sorted(counts.items())),
                                notices=tuple(sorted(notices)), **state)

    if not _word(str(details.get("listing_id") or "")):
        return outcome("subject_identity_unknown")
    if any(_word(details.get(k)) is None for k in ("brand", "model")) or _year(details.get("year")) is None:
        return outcome("subject_core_features_missing")
    if price is None or price <= 0 or currency is None:
        return outcome("subject_price_or_currency_unknown")
    subject_time_error, _ = _row_meta(details, now, policy.max_subject_price_age_seconds)
    if subject_time_error:
        return outcome("subject_" + subject_time_error)
    flags = price_flags(details)
    if flags:
        notices.update(flags)
        return outcome("subject_price_ambiguous", "suspicious_price")
    # Use the latest *known* snapshot before filtering, so an outdated valid
    # snapshot cannot silently replace a newer invalid/repriced observation.
    latest = {}
    subject_id = str(details["listing_id"])
    subject_duplicate = _word(details.get("duplicate_key"))
    for index, row in enumerate(peers):
        state["input_count"] = index + 1
        if index >= policy.max_peers:
            return outcome("peer_input_limit_exceeded")
        if not isinstance(row, Mapping):
            counts["invalid_peer"] += 1
            continue
        peer_id = str(row.get("listing_id") or "")
        if not peer_id:
            counts["peer_identity_unknown"] += 1
            continue
        group = _word(row.get("duplicate_key"))
        if peer_id == subject_id or (group is not None and group == subject_duplicate):
            counts["subject_or_duplicate_leakage"] += 1
            continue
        observed = _time(row.get("observed_at"))
        if observed is None:
            counts["observation_time_unknown"] += 1
            continue
        if observed > now:
            counts["future_observation"] += 1
            continue
        if peer_id in latest:
            counts["duplicate_snapshot"] += 1
            old_time = _time(latest[peer_id].get("observed_at"))
            if observed == old_time:
                # Conflicting equal-time price/features cannot be resolved by
                # list order. Keep a marked row that is excluded below.
                if _valuation_snapshot(row) != _valuation_snapshot(latest[peer_id]):
                    latest[peer_id] = dict(row, _conflicting_snapshot=True)
                continue
            if observed < old_time:
                continue
        latest[peer_id] = row
    grouped, ungrouped = {}, []
    for peer_id, row in sorted(latest.items()):
        group = _word(row.get("duplicate_key"))
        if group is None:
            ungrouped.append(row)
            continue
        if group in grouped:
            counts["duplicate_vehicle"] += 1
            current = grouped[group]
            current_time, row_time = _time(current["observed_at"]), _time(row["observed_at"])
            if row_time == current_time:
                if _valuation_snapshot(row) != _valuation_snapshot(current):
                    grouped[group] = dict(row, _conflicting_snapshot=True)
                continue
            if row_time < current_time:
                continue
        grouped[group] = row
    selected = []
    for row in ungrouped + list(grouped.values()):
        if row.get("_conflicting_snapshot"):
            counts["conflicting_snapshot"] += 1
            continue
        time_error, timestamps = _row_meta(row, now, policy.max_peer_age_seconds)
        if time_error:
            counts[time_error] += 1
            continue
        value, row_currency = _price(row)
        if value is None or value <= 0:
            counts["invalid_price"] += 1
            continue
        if row_currency != currency:
            counts["currency_mismatch_or_unknown"] += 1
            continue
        if price_flags(row):
            counts["ambiguous_peer_price"] += 1
            continue
        mismatch = _mismatch(details, row, policy)
        if mismatch:
            counts[mismatch] += 1
            continue
        selected.append((row, value, now - timestamps[1]))
    # Regions can materially change asking prices. Prefer a sufficiently sized
    # local sample; otherwise expose a regional mix without fictitious factors.
    region = _word(details.get("region"))
    regional = [item for item in selected if region is not None and _word(item[0].get("region")) == region]
    if len(regional) >= policy.min_peers:
        counts["outside_sufficient_regional_sample"] += len(selected) - len(regional)
        selected = regional
    elif region is not None and len(regional) < len(selected):
        notices.add("mixed_or_unknown_peer_regions")
    if len(selected) < policy.min_peers:
        return outcome("too_few_comparable_peers", sample_count=len(selected),
                       region_sample_count=len(regional))
    prices = [item[1] for item in selected]
    median = _quantile(prices, D("0.5"))
    mad = _quantile([abs(p - median) for p in prices], D("0.5"))
    # MAD=0 often means common round-number asking prices. Keep a conservative
    # median-relative band instead of declaring every different amount an outlier.
    band = max(mad * 4, median * D("0.35"))
    retained = [item for item in selected if abs(item[1] - median) <= band]
    counts["price_outlier"] += len(selected) - len(retained)
    if len(retained) < policy.min_peers:
        return outcome("too_few_peers_after_outliers", sample_count=len(retained))
    prices = [item[1] for item in retained]
    lower, middle, upper = (_quantile(prices, q) for q in (D("0.25"), D("0.5"), D("0.75")))
    oldest, newest = max(item[2] for item in retained), min(item[2] for item in retained)
    state.update(sample_count=len(retained), lower_asking_price=lower,
                 median_asking_price=middle, upper_asking_price=upper,
                 peer_age_min_seconds=newest, peer_age_max_seconds=oldest,
                 region_sample_count=sum(_word(item[0].get("region")) == region and region is not None
                                         for item in retained))
    if any(_missing(item[0]) for item in retained):
        notices.add("peer_optional_fields_missing")
    if (upper - lower) / middle > policy.maximum_relative_spread:
        return outcome("peer_price_dispersion_too_wide")
    reference = _quantile(prices, policy.reference_quantile)
    if price / reference < policy.suspicious_price_ratio:
        return outcome("subject_price_extreme_relative_to_peers", "suspicious_price")
    discount = (reference - price) / reference * 100
    state.update(reference_price=reference, discount_percent=discount)
    return ValuationOutcome(status="estimated",
                            classification="plausible_undervalued" if discount > 0 else "not_undervalued",
                            reason="experimental_peer_asking_estimate",
                            excluded=tuple(sorted((k, v) for k, v in counts.items() if v)),
                            notices=tuple(sorted(notices)), **state)


def evaluate_labeled_cases(cases, *, min_discount_percent=Decimal("10")):
    """Count independent caller-supplied labels, retaining unknown denominators.

    Each case is {outcome: ValuationOutcome, should_qualify: bool|None}.
    Unknown positive labels count as missed opportunities, separately reported
    from definite model negatives. No inferred/reused appraisal labels exist.
    This helper measures only the supplied sample, not source-wide quality.
    """
    threshold = _number(min_discount_percent)
    if threshold is None or not 0 <= threshold < 100:
        raise ValueError("invalid_discount_threshold")
    result = Counter(total=0, labeled=0, unlabeled=0, positive_labels=0,
                     negative_labels=0, true_positive=0, false_positive=0,
                     true_negative=0, false_negative=0, unknown=0,
                     unknown_positive=0, unknown_negative=0, predicted_positive=0)
    for case in cases:
        value, label = case["outcome"], case.get("should_qualify")
        if not isinstance(value, ValuationOutcome) or (label is not None and type(label) is not bool):
            raise ValueError("invalid_labeled_case")
        result["total"] += 1
        unknown = value.status != "estimated"
        positive = not unknown and value.discount_percent is not None and value.discount_percent >= threshold
        result["unknown"] += unknown
        result["predicted_positive"] += positive
        if label is None:
            result["unlabeled"] += 1
            continue
        result["labeled"] += 1
        result["positive_labels" if label else "negative_labels"] += 1
        if unknown:
            result["unknown_positive" if label else "unknown_negative"] += 1
        elif label:
            result["true_positive" if positive else "false_negative"] += 1
        else:
            result["false_positive" if positive else "true_negative"] += 1
    result["missed_positive_total"] = result["false_negative"] + result["unknown_positive"]
    def ratio(numerator, denominator):
        return None if denominator == 0 else float(D(numerator) / denominator)
    out = dict(result)
    out["opportunity_recall_including_unknown"] = ratio(result["true_positive"], result["positive_labels"])
    out["precision_on_labeled_predictions"] = ratio(result["true_positive"], result["true_positive"] + result["false_positive"])
    out["unknown_fraction"] = ratio(result["unknown"], result["total"])
    out["quality_scope"] = "supplied_labels_only_not_live_source_validation"
    return out
