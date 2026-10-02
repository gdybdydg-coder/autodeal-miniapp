import unittest
from dataclasses import replace
from decimal import Decimal

from experiments.free_search.filter_gate import PublicEvidence, SearchFilter, Span, gate


class FilterGateTests(unittest.TestCase):
    def setUp(self):
        self.filters = SearchFilter(
            regions=frozenset({"ternopilska", "khmelnytska"}),
            price_usd=Span(Decimal("3000"), Decimal("15000")),
            year_min=2005,
            year_max=2020,
            bodies=frozenset({"wagon"}),
            fuels=frozenset({"diesel"}),
            transmissions=frozenset({"automatic"}),
            mileage_min_km=50000,
            mileage_max_km=250000,
            min_discount_percent=Decimal("15"),
        )
        self.evidence = PublicEvidence(
            listing_id="40503544", publication_proven=True, active=True,
            category="passenger", price_usd=Decimal("10000"),
            region="ternopilska", year=2015, body="wagon", fuel="diesel",
            transmission="automatic", mileage_km=150000,
            abroad=False, needs_customs=False,
        )

    def test_match_only_authorizes_valuation(self):
        result = gate(self.evidence, self.filters)
        self.assertTrue(result.eligible_for_valuation)
        self.assertFalse(result.ready_for_delivery)
        self.assertEqual(result.blockers, ())
        self.assertIn("valuation_required", result.notices)

    def test_missing_optional_characteristics_do_not_hide_car(self):
        sparse = replace(self.evidence, year=None, body=None, fuel=None,
                         transmission=None, mileage_km=None)
        self.assertTrue(gate(sparse, self.filters).eligible_for_valuation)

    def test_every_known_optional_conflict_blocks(self):
        cases = {
            "year_mismatch": {"year": 2000},
            "body_mismatch": {"body": "sedan"},
            "fuel_mismatch": {"fuel": "petrol"},
            "transmission_mismatch": {"transmission": "manual"},
            "mileage_mismatch": {"mileage_km": 300000},
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, gate(replace(self.evidence, **changes), self.filters).blockers)

    def test_region_is_required_only_for_region_filtered_search(self):
        missing = replace(self.evidence, region=None)
        self.assertIn("region_not_proven", gate(missing, self.filters).blockers)
        any_region = replace(self.filters, regions=frozenset())
        self.assertTrue(gate(missing, any_region).eligible_for_valuation)

    def test_wrong_region_blocks(self):
        result = gate(replace(self.evidence, region="kyivska"), self.filters)
        self.assertEqual(result.blockers, ("region_mismatch",))

    def test_known_abroad_and_customs_flags_block(self):
        self.assertIn("abroad", gate(replace(self.evidence, abroad=True), self.filters).blockers)
        self.assertIn("needs_customs", gate(replace(self.evidence, needs_customs=True), self.filters).blockers)

    def test_unknown_abroad_and_customs_are_explicit_notices(self):
        result = gate(replace(self.evidence, abroad=None, needs_customs=None), self.filters)
        self.assertTrue(result.eligible_for_valuation)
        self.assertIn("abroad_unknown", result.notices)
        self.assertIn("customs_unknown", result.notices)

    def test_damage_and_repair_parts_never_block(self):
        result = gate(replace(self.evidence, damaged=True, repair_parts=True), self.filters)
        self.assertTrue(result.eligible_for_valuation)
        self.assertIn("damage", result.notices)
        self.assertIn("repair_parts", result.notices)

    def test_publication_category_and_active_state_are_hard_gates(self):
        cases = [
            (replace(self.evidence, publication_proven=False), "publication_not_proven"),
            (replace(self.evidence, category=None), "category_not_proven"),
            (replace(self.evidence, category="motorcycle"), "not_passenger"),
            (replace(self.evidence, active=None), "active_status_not_proven"),
            (replace(self.evidence, active=False), "inactive"),
        ]
        for evidence, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason, gate(evidence, self.filters).blockers)

    def test_price_must_be_exact_positive_decimal_and_match_filter(self):
        cases = [
            (None, "positive_current_price_not_proven"),
            (Decimal("0"), "positive_current_price_not_proven"),
            (Decimal("NaN"), "positive_current_price_not_proven"),
            (10000, "positive_current_price_not_proven"),
            (Decimal("2999.99"), "price_mismatch"),
            (Decimal("15000.01"), "price_mismatch"),
        ]
        for price, reason in cases:
            with self.subTest(price=price):
                self.assertIn(reason, gate(replace(self.evidence, price_usd=price), self.filters).blockers)

    def test_invalid_listing_id_blocks(self):
        for listing_id in ("", "0", "01", "abc", "1" * 13):
            with self.subTest(listing_id=listing_id):
                self.assertIn("invalid_listing_id",
                              gate(replace(self.evidence, listing_id=listing_id), self.filters).blockers)

    def test_no_input_can_become_delivery_ready(self):
        self.assertFalse(gate(self.evidence, self.filters).ready_for_delivery)


if __name__ == "__main__":
    unittest.main()
