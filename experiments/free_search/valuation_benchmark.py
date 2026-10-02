"""Offline aggregate benchmark on privately supplied historical observations.

No fixture data, database access, external request or paid key is included.
Historical provider appraisals are labels only, never estimator features.
Run: python -m experiments.free_search.valuation_benchmark --help
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import median

from experiments.free_search.valuation import estimate, evaluate_labeled_cases


MAX_INPUT_BYTES = 100 * 1024 * 1024


def canonical(item):
    """Project a historical fixture onto permitted asking-price features."""
    if "features" not in item:
        # Feature-only peer input: whitelist avoids passing arbitrary labels.
        keys = ("listing_id", "brand", "model", "generation", "year", "engine_cc",
                "transmission", "fuel", "region", "mileage_km", "body", "price_usd",
                "currency", "observed_at", "publication_at")
        row = {key: item[key] for key in keys if key in item}
        if isinstance(row.get("fuel"), str):
            row["fuel"] = row["fuel"].split(",", 1)[0].strip()
        return row
    features = item["features"]

    def token(key):
        value = features.get(key)
        return None if value is None else str(value)

    return dict(listing_id=str(item["source_id"]), brand=features.get("brand"),
                model=features.get("model"), generation=token("generation_id"),
                year=features.get("year"), engine_cc=features.get("engine_cc"),
                transmission=features.get("transmission"),
                fuel=(features.get("fuel") or "").split(",", 1)[0].strip() or None,
                region=features.get("region"), mileage_km=features.get("mileage"),
                body=features.get("body"), price=Decimal(str(item["asking_price_usd"])),
                currency="USD", observed_at=item["candidate_observed_at"])


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def benchmark(target_data, peer_data=None):
    """Keep target outcomes/labels and past peer features strictly separate."""
    rows = [(item, canonical(item)) for item in target_data["items"]]
    supplied_peers = target_data if peer_data is None else peer_data
    by_model = defaultdict(list)
    for item in supplied_peers["items"]:
        row = canonical(item)
        by_model[(row["brand"], row["model"])].append(row)
    results = []
    for item, row in rows:
        label = item["historical_label"]
        asof = max(row["observed_at"], label["quote_observed_at"])
        earlier = [peer for peer in by_model[(row["brand"], row["model"])]]
        earlier = [peer for peer in earlier if peer["observed_at"] <= row["observed_at"]]
        outcome = estimate(row, earlier, asof)
        lower = Decimal(str(label["lower_usd"]))
        if not lower.is_finite() or lower <= 0:
            raise ValueError("invalid_historical_label")
        reference = lower * Decimal(".95")
        discount = (reference - row["price"]) / reference * 100
        results.append((outcome, reference, discount))
    metrics = {}
    for threshold in (0, 5, 10, 15, 20):
        cases = [dict(outcome=outcome, should_qualify=discount >= threshold)
                 for outcome, reference, discount in results]
        metrics[str(threshold)] = evaluate_labeled_cases(cases, min_discount_percent=threshold)
    errors = [abs(outcome.reference_price - reference) / reference * 100
              for outcome, reference, discount in results if outcome.status == "estimated"]
    counts = Counter(outcome.sample_count for outcome, _, _ in results)
    return {
        "experiment": "experimental_peer_asking_v1 default policy, fixed before measuring sample",
        "sample_source": target_data.get("provenance", "caller_supplied_private_observations"),
        "sample_selection": target_data.get("selection", "unspecified_do_not_claim_representative"),
        "sample_count": len(rows), "peer_basket_count": len(supplied_peers["items"]),
        "eligible_in_snapshot": target_data.get("eligible_records"),
        "period_candidate_observed_utc": None if not rows else [
            _iso(min(row["observed_at"] for _, row in rows)),
            _iso(max(row["observed_at"] for _, row in rows))],
        "brands": len({row["brand"] for _, row in rows}),
        "brand_models": len({(row["brand"], row["model"]) for _, row in rows}),
        "status_counts": dict(Counter(outcome.status for outcome, _, _ in results)),
        "reason_counts": dict(Counter(outcome.reason for outcome, _, _ in results)),
        "comparable_sample_count_histogram": dict(sorted(counts.items())),
        "metrics_at_discount_threshold_percent": metrics,
        "median_absolute_percent_difference_from_historical_production_reference":
            None if not errors else float(median(errors)),
        "external_calls": 0, "new_paid_api_calls": 0, "real_telegram_calls": 0,
        "label_basis": "Historical provider lower bound times0.95; proxy agreement only, not verified sale value or ground truth bargain.",
        "leakage_guards": [
            "Subject excluded by listing_id.",
            "No provider label or market/reference values passed to estimator.",
            "Peers require candidate_observed_at <= target candidate_observed_at.",
            "as_of=max(target candidate_observed_at,target quote_observed_at).",
            "Generation IDs normalized to strings; transmission/region use same-source names; fuel displacement suffix stripped because engine_cc is separate; mileage already kilometres.",
            "No parameter tuning, model training or new paid data acquisition in this benchmark.",
        ],
        "limitations": list(target_data.get("limitations", [])) + [
            "Snapshot contains only latest retained candidate/quote, no complete price revision history or independent source universe.",
            "Sample originated from paid discovery; proves offline algorithm behavior, not free source coverage.",
            "Sparse regional/model/time cohorts can leave many requests without enough peers.",
            "No descriptions/price context available: non-full price checks only numeric placeholders in this historical benchmark.",
            "Missing estimate remains unknown, including provider-positive opportunities; not dropped from denominator.",
            "Thresholds0/5/10/15/20 are comparison scenarios; actual historical user thresholds unavailable.",
        ],
    }


def read_input(path):
    source = Path(path)
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("benchmark_input_too_large")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("invalid_benchmark_input")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True, help="Private historical target JSON with labels")
    parser.add_argument("--peers", help="Optional larger private JSON of feature-only historical peers")
    parser.add_argument("--output", help="Write aggregate JSON; contains no listing/user IDs or observations")
    args = parser.parse_args()
    result = benchmark(read_input(args.targets), read_input(args.peers) if args.peers else None)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
