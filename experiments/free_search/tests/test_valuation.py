"""Controlled fixtures only: assertions are not live model-quality evidence."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import unittest

from experiments.free_search.valuation import (
    PROVENANCE, ValuationPolicy, estimate, evaluate_labeled_cases, price_flags,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp()


def car(identity="500", price="3900", **kw):
    values = dict(listing_id=identity, brand="Volkswagen", model="Passat",
                  generation="B5+", year=2003, engine_liters="1.8", fuel="petrol",
                  transmission="manual", mileage_km=250000, region="Chernivtsi",
                  price=D(price), currency="USD", observed_at=NOW - 60,
                  price_kind="whole_vehicle", whole_vehicle=True)
    values.update(kw)
    return values


def peers(**kw):
    return [car(str(i + 1), amount, observed_at=NOW - (i + 1) * 3600, **kw)
            for i, amount in enumerate(("5000", "5200", "5400", "5500", "5600", "5800", "6000"))]


class PeerValuationTest(unittest.TestCase):
    def test_estimate_is_explicitly_experimental_asking_price(self):
        result = estimate(car(), peers(), NOW)
        self.assertEqual((result.status, result.classification), ("estimated", "plausible_undervalued"))
        self.assertEqual(result.reference_price, D("5300"))
        self.assertEqual(result.sample_count, 7)
        self.assertEqual(result.provenance, PROVENANCE)
        self.assertFalse(result.production_approved)
        self.assertEqual(result.confidence, "uncalibrated")
        self.assertIn("asking_prices_not_sales", result.notices)
        self.assertEqual(result.peer_age_max_seconds, 7 * 3600)
        self.assertEqual(result.reference_price_usd, D("5300"))
        # No -5% adjustment silently copied from the unrelated production rule.
        self.assertNotEqual(result.reference_price, D("5300") * D("0.95"))
        self.assertEqual(json.loads(json.dumps(result.as_dict()))["reference_price"], "5300.00")

    def test_no_discount_not_unknown(self):
        result = estimate(car(price="6000"), peers(), NOW)
        self.assertEqual(result.classification, "not_undervalued")
        self.assertEqual(result.status, "estimated")
        self.assertLess(result.discount_percent, 0)

    def test_damaged_whole_car_and_for_parts_not_excluded(self):
        for title in ("Цілий автомобіль на запчастини, потребує ремонту",
                      "Продам авто полностью на запчасти", "Whole car for parts"):
            with self.subTest(title=title):
                value = car(price="1200", damaged=True, repair_parts=True, title=title)
                result = estimate(value, peers(), NOW)
                self.assertEqual(result.status, "estimated")
                self.assertEqual(result.classification, "plausible_undervalued")
                self.assertIn("whole_car_condition_not_excluded", result.notices)

    def test_missing_fuel_gear_photo_not_automatic_block(self):
        subject = car(fuel=None, transmission=None, generation=None, region=None,
                      engine_liters=None, mileage_km=None)
        result = estimate(subject, peers(), NOW)
        self.assertEqual(result.status, "estimated")
        self.assertEqual(set(result.missing_fields), {"fuel", "transmission", "generation", "region", "engine", "mileage_km"})
        self.assertIn("subject_optional_fields_missing", result.notices)

    def test_missing_peer_optional_fields_are_visible(self):
        values = peers(fuel=None, transmission=None, generation=None, region=None,
                       engine_liters=None, mileage_km=None)
        result = estimate(car(), values, NOW)
        self.assertEqual(result.status, "estimated")
        self.assertIn("peer_optional_fields_missing", result.notices)
        self.assertIn("mixed_or_unknown_peer_regions", result.notices)

    def test_known_incompatible_fields_are_not_comparables(self):
        incompatible = ({"brand": "Audi"}, {"model": "Golf"}, {"generation": "B8"},
                        {"year": 2008}, {"engine_liters": "3.0"}, {"fuel": "diesel"},
                        {"transmission": "automatic"}, {"mileage_km": 10000})
        for fields in incompatible:
            with self.subTest(fields=fields):
                result = estimate(car(), peers(**fields), NOW)
                self.assertEqual(result.status, "unknown")
                self.assertEqual(result.reason, "too_few_comparable_peers")
                self.assertEqual(result.sample_count, 0)

    def test_mileage_is_km_not_thousands(self):
        result = estimate(car(mileage_km=250), peers(), NOW)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(dict(result.excluded)["mileage_mismatch"], 7)
        result = estimate(car(mileage_km=D("250000")), peers(), NOW)
        self.assertEqual(result.status, "estimated")

    def test_liters_and_cc_are_equivalent(self):
        subject = car(engine_liters=None, engine_cc=1800)
        self.assertEqual(estimate(subject, peers(), NOW).status, "estimated")

    def test_upfront_installment_part_placeholder_are_unknown_not_deals(self):
        examples = (
            ("Перший внесок 3900 USD", "price_upfront"),
            ("Аванс 3900 доларів", "price_upfront"),
            ("Down payment $3900", "price_upfront"),
            ("3900 гривень на місяць", "price_installment"),
            ("Щомісячний платіж", "price_installment"),
            ("$3900/month", "price_installment"),
            ("Ціна за деталь", "price_part"),
            ("Ціна за двигун", "price_part"),
            ("Price for a part", "price_part"),
            ("Ціна умовна", "price_placeholder"),
        )
        for text, flag in examples:
            with self.subTest(text=text):
                subject = car(price_context=text)
                self.assertIn(flag, price_flags(subject))
                result = estimate(subject, peers(), NOW)
                self.assertEqual((result.status, result.classification), ("unknown", "suspicious_price"))
                self.assertIsNone(result.reference_price)
                self.assertIn(flag, result.notices)

    def test_credit_available_is_not_mistaken_for_installment_amount(self):
        for text in ("Кредит можливий. Повна ціна 3900 USD", "Можно купить в кредит"):
            self.assertEqual(price_flags(car(price_context=text)), ())
            self.assertEqual(estimate(car(price_context=text), peers(), NOW).status, "estimated")

    def test_explicit_price_kind_and_non_vehicle_flags(self):
        for kind in ("upfront", "installment", "part", "placeholder"):
            with self.subTest(kind=kind):
                self.assertIn("price_" + kind, price_flags(car(price_kind=kind)))
                self.assertEqual(estimate(car(price_kind=kind), peers(), NOW).status, "unknown")
        self.assertIn("not_whole_vehicle", price_flags(car(whole_vehicle=False)))
        self.assertEqual(estimate(car(price_kind="loan_balance"), peers(), NOW).status, "unknown")

    def test_placeholder_range_and_extreme_ratio_are_review_cases(self):
        result = estimate(car(price="1"), peers(), NOW)
        self.assertEqual(result.reason, "subject_price_ambiguous")
        self.assertIn("price_placeholder_range", result.notices)
        result = estimate(car(price="200"), peers(), NOW)
        self.assertEqual(result.reason, "subject_price_extreme_relative_to_peers")
        self.assertEqual(result.classification, "suspicious_price")
        self.assertEqual(result.sample_count, 7)
        self.assertIsNone(result.reference_price)
        # An entire damaged car at $900 is not automatically discarded.
        self.assertEqual(estimate(car(price="900", damaged=True), peers(), NOW).status, "estimated")

    def test_price_and_core_field_validation(self):
        changes = ({"price": D("NaN")}, {"price": D("Infinity")}, {"price": -1},
                   {"price": True}, {"currency": "BTC"}, {"brand": None},
                   {"model": None}, {"year": None}, {"listing_id": None})
        for fields in changes:
            with self.subTest(fields=fields):
                self.assertEqual(estimate(car(**fields), peers(), NOW).status, "unknown")

    def test_price_usd_adapter_needs_no_assumed_fx(self):
        subject = car()
        subject.pop("price")
        subject.pop("currency")
        subject["price_usd"] = D("3900")
        self.assertEqual(estimate(subject, peers(), NOW).reference_price_usd, D("5300"))
        result = estimate(car(currency="UAH"), peers(), NOW)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(dict(result.excluded)["currency_mismatch_or_unknown"], 7)
        result = estimate(car(currency="EUR"), peers(currency="EUR"), NOW)
        self.assertEqual(result.status, "estimated")
        self.assertEqual(result.currency, "EUR")
        self.assertIsNone(result.reference_price_usd)

    def test_subject_price_must_be_current_observed_not_future(self):
        variants = (
            ({"observed_at": None}, "subject_observation_time_unknown"),
            ({"observed_at": NOW + 1}, "subject_future_observation"),
            ({"observed_at": NOW - 90000}, "subject_stale_price"),
            ({"price_observed_at": NOW - 90000}, "subject_stale_price"),
            ({"observed_at": NOW - 2, "price_observed_at": NOW - 1}, "subject_future_observation"),
            ({"publication_at": NOW + 10}, "subject_publication_time_conflict"),
        )
        for fields, reason in variants:
            with self.subTest(fields=fields):
                self.assertEqual(estimate(car(**fields), peers(), NOW).reason, reason)
        self.assertEqual(estimate(car(observed_at=datetime.fromtimestamp(NOW - 2, timezone.utc)), peers(), NOW).status, "estimated")
        with self.assertRaisesRegex(ValueError, "invalid_as_of"):
            estimate(car(), peers(), datetime(2026, 10, 2))

    def test_stale_future_peers_do_not_leak_into_reference(self):
        clean = peers()
        stale = [dict(item, listing_id="9" + item["listing_id"], observed_at=NOW - 31 * 86400) for item in peers()]
        future = [dict(item, listing_id="8" + item["listing_id"], price=D("50000"), observed_at=NOW + 1) for item in peers()]
        result = estimate(car(), clean + stale + future, NOW)
        self.assertEqual(result.reference_price, D("5300"))
        self.assertEqual(dict(result.excluded)["stale_price"], 7)
        self.assertEqual(dict(result.excluded)["future_observation"], 7)
        self.assertEqual(estimate(car(), stale, NOW).status, "unknown")

    def test_subject_and_same_vehicle_cannot_be_own_peer(self):
        subject = car(duplicate_key="same-vehicle")
        values = peers() + [subject, car("700", "50000", duplicate_key="same-vehicle")]
        result = estimate(subject, values, NOW)
        self.assertEqual(result.sample_count, 7)
        self.assertEqual(dict(result.excluded)["subject_or_duplicate_leakage"], 2)
        self.assertEqual(result.reference_price, D("5300"))

    def test_duplicates_do_not_inflate_sample_count(self):
        values = [car("1", "5000"), car("1", "5100", observed_at=NOW - 1),
                  car("2", "5200", duplicate_key="one-vehicle"),
                  car("3", "5200", duplicate_key="one-vehicle")]
        result = estimate(car(), values, NOW)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(dict(result.excluded)["duplicate_snapshot"], 1)
        self.assertEqual(dict(result.excluded)["duplicate_vehicle"], 1)

    def test_same_vehicle_equal_time_price_conflict_is_unknown(self):
        values = [car(str(i), "5000", duplicate_key="same-vehicle") for i in range(1, 5)]
        values.append(car("10", "6000", duplicate_key="same-vehicle"))
        result = estimate(car(), values, NOW)
        self.assertEqual(result.sample_count, 0)
        self.assertEqual(dict(result.excluded)["conflicting_snapshot"], 1)

    def test_latest_known_snapshot_used_and_new_invalid_snapshot_not_hidden(self):
        historical = peers()
        future = dict(historical[0], price=D("30000"), observed_at=NOW + 1)
        self.assertEqual(estimate(car(), historical + [future], NOW).reference_price, D("5300"))
        changes = [dict(row, price=D("NaN"), observed_at=NOW - 1) for row in historical[:3]]
        result = estimate(car(), historical + changes, NOW)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.sample_count, 4)
        self.assertEqual(dict(result.excluded)["invalid_price"], 3)

    def test_equal_time_conflicts_fail_closed_regardless_of_input_order(self):
        values = peers()
        conflicting = [dict(row, price=D("99999")) for row in values[:3]]
        forward = estimate(car(), values + conflicting, NOW)
        backward = estimate(car(), conflicting + values, NOW)
        self.assertEqual(forward.status, "unknown")
        self.assertEqual(forward.sample_count, backward.sample_count)
        self.assertEqual(dict(forward.excluded)["conflicting_snapshot"], 3)

    def test_paid_appraisal_and_truth_labels_have_no_effect(self):
        original = peers()
        annotated = [dict(row, paid_appraisal=9999999, ground_truth="positive", should_qualify=True) for row in original]
        plain = estimate(car(), original, NOW)
        augmented = estimate(dict(car(), paid_appraisal=1), original + annotated, NOW)
        self.assertEqual(plain.reference_price, augmented.reference_price)
        self.assertEqual(augmented.sample_count, len(original))
        self.assertNotIn("conflicting_snapshot", dict(augmented.excluded))
        self.assertNotIn("paid_appraisal", json.dumps(augmented.as_dict()))

    def test_anomalous_peer_prices_removed_not_fake_deals(self):
        values = peers() + [car("80", "200"), car("90", "50000")]
        result = estimate(car(), values, NOW)
        self.assertEqual(result.sample_count, 7)
        self.assertEqual(result.reference_price, D("5300"))
        self.assertEqual(dict(result.excluded)["price_outlier"], 2)
        values = peers() + [car("80", "2000", price_kind="installment")]
        result = estimate(car(), values, NOW)
        self.assertEqual(dict(result.excluded)["ambiguous_peer_price"], 1)

    def test_zero_mad_round_prices_still_can_estimate(self):
        values = [car(str(i), "5000") for i in range(1, 8)] + [car("50", "60000")]
        result = estimate(car(), values, NOW)
        self.assertEqual(result.status, "estimated")
        self.assertEqual(result.reference_price, D("5000"))
        self.assertEqual(dict(result.excluded)["price_outlier"], 1)

    def test_wide_comparable_spread_is_unknown(self):
        values = [car(str(i + 1), str(price)) for i, price in enumerate((3000, 3500, 4000, 6000, 8000, 8500, 9000))]
        result = estimate(car(), values, NOW)
        self.assertEqual(result.reason, "peer_price_dispersion_too_wide")
        self.assertIsNone(result.reference_price)

    def test_region_matters_without_invented_price_adjustment(self):
        local = peers()
        other = [dict(row, listing_id="9" + row["listing_id"], region="Kyiv", price=row["price"] * 2) for row in peers()]
        result = estimate(car(), local + other, NOW)
        self.assertEqual(result.reference_price, D("5300"))
        self.assertEqual(result.region_sample_count, 7)
        self.assertEqual(dict(result.excluded)["outside_sufficient_regional_sample"], 7)
        result = estimate(car(), peers(region="Kyiv"), NOW)
        self.assertEqual(result.status, "estimated")
        self.assertIn("mixed_or_unknown_peer_regions", result.notices)
        self.assertEqual(result.region_sample_count, 0)

    def test_input_limit_is_not_silently_successful_partial_sample(self):
        policy = replace(ValuationPolicy(), max_peers=5)
        result = estimate(car(), iter(peers()), NOW, policy)
        self.assertEqual(result.reason, "peer_input_limit_exceeded")
        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.reference_price)

    def test_invalid_policy_rejected(self):
        for change in ({"min_peers": 1}, {"min_peers": True}, {"max_peer_age_seconds": 0},
                       {"reference_quantile": 0.25}, {"suspicious_price_ratio": D("NaN")}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "invalid_valuation_policy"):
                replace(ValuationPolicy(), **change)

    def test_unknowns_and_false_negatives_stay_in_metrics(self):
        good = estimate(car(), peers(), NOW)
        rejected = estimate(car(price="6000"), peers(), NOW)
        unknown = estimate(car(), [], NOW)
        values = [dict(outcome=good, should_qualify=True),
                  dict(outcome=good, should_qualify=False),
                  dict(outcome=rejected, should_qualify=True),
                  dict(outcome=rejected, should_qualify=False),
                  dict(outcome=unknown, should_qualify=True),
                  dict(outcome=unknown, should_qualify=False),
                  dict(outcome=unknown, should_qualify=None)]
        result = evaluate_labeled_cases(values)
        for key in ("true_positive", "false_positive", "true_negative", "false_negative",
                    "unknown_positive", "unknown_negative", "unlabeled"):
            self.assertEqual(result[key], 1, key)
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["unknown"], 3)
        self.assertEqual(result["missed_positive_total"], 2)
        self.assertAlmostEqual(result["opportunity_recall_including_unknown"], 1 / 3)
        self.assertEqual(result["precision_on_labeled_predictions"], .5)
        self.assertIn("not_live_source_validation", result["quality_scope"])

    def test_empty_metric_denominators_are_unknown_not_perfect(self):
        result = evaluate_labeled_cases([])
        self.assertIsNone(result["opportunity_recall_including_unknown"])
        self.assertIsNone(result["precision_on_labeled_predictions"])
        self.assertIsNone(result["unknown_fraction"])
        with self.assertRaisesRegex(ValueError, "invalid_labeled_case"):
            evaluate_labeled_cases([dict(outcome=estimate(car(), [], NOW), should_qualify="yes")])


if __name__ == "__main__":
    unittest.main()
