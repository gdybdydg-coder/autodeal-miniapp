import ast
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from experiments.free_search.filter_gate import SearchFilter, gate
from experiments.free_search.public_cards import Candidate
from experiments.free_search.public_details import PublicDetails
from experiments.free_search.visible_adapter import (
    VisibleParseError, adapt_candidate, parse_visible_facts,
)


KYIV = ZoneInfo("Europe/Kyiv")


def page(category="Легкові з пробігом", region="Тернопільська область",
         created="01.10.2026", listing_id="40503544", footer=""):
    return f"""<!doctype html><html><body>
      <a> AUTO.RIA </a><a>{category}</a><a>{region}</a><a>Тернопіль</a>
      <h1>Fixture vehicle</h1>
      <p>Оголошення створене {created}</p><p>ID авто {listing_id}</p>
      {footer}</body></html>"""


def details(**changes):
    values = dict(listing_id="40503544", brand="Test", model="Fixture", year=2015,
                  body="Універсал", fuel="Дизель", transmission="Автомат",
                  mileage_km=Decimal("150000"), price=Decimal("10000"),
                  currency="USD", availability="active", condition=None,
                  missing=(), issues=(), provenance=(("price", "public_jsonld"),),
                  ready_for_delivery=False)
    values.update(changes)
    return PublicDetails(**values)


def candidate(**changes):
    values = dict(listing_id="40503544", url="https://auto.ria.com/uk/auto_test_40503544.html",
                  added_at=datetime(2026, 10, 1, 3, 7, tzinfo=KYIV).timestamp(),
                  preview_usd=Decimal("10000"))
    values.update(changes)
    return Candidate(**values)


class VisibleAdapterTests(unittest.TestCase):
    def test_exact_visible_facts(self):
        facts = parse_visible_facts(page(), "40503544")
        self.assertEqual(facts.category, "passenger")
        self.assertEqual(facts.region, "Тернопільська область")
        self.assertEqual(str(facts.created_date), "2026-10-01")
        self.assertEqual(facts.listing_id, "40503544")
        self.assertEqual(facts.issues, ())

    def test_known_non_passenger_category_is_preserved(self):
        facts = parse_visible_facts(page(category="Мотоцикли з пробігом"), "40503544")
        self.assertEqual(facts.category, "motorcycle")
        adapted = adapt_candidate(candidate(), details(), facts)
        self.assertIn("not_passenger", gate(adapted.evidence, SearchFilter()).blockers)

    def test_missing_category_fails_closed(self):
        facts = parse_visible_facts(page(category="Вживані авто"), "40503544")
        self.assertIsNone(facts.category)
        self.assertIn("missing_category", facts.issues)

    def test_footer_category_after_title_is_not_evidence(self):
        facts = parse_visible_facts(page(category="Вживані авто",
            footer="<footer><a>Легкові з пробігом</a></footer>"), "40503544")
        self.assertIsNone(facts.category)

    def test_script_text_is_not_visible_evidence(self):
        spoof = """<script type="application/ld+json">
          {"fake":"Легкові з пробігом Оголошення створене 01.10.2026 ID авто 40503544"}
        </script><a>Тернопільська область</a><h1>Fixture</h1>"""
        facts = parse_visible_facts(spoof, "40503544")
        self.assertIsNone(facts.category)
        self.assertIsNone(facts.created_date)
        self.assertIsNone(facts.listing_id)
        self.assertIn("missing_category", facts.issues)
        self.assertIn("missing_created_date", facts.issues)
        self.assertIn("missing_listing_id", facts.issues)

    def test_conflicting_pre_title_categories_and_regions_fail_closed(self):
        html = page().replace("<h1>",
            "<a>Мотоцикли з пробігом</a><a>Хмельницька область</a><h1>")
        facts = parse_visible_facts(html, "40503544")
        self.assertIsNone(facts.category)
        self.assertIsNone(facts.region)
        self.assertIn("conflicting_category", facts.issues)
        self.assertIn("conflicting_region", facts.issues)

    def test_listing_identity_mismatch_and_conflict_are_explicit(self):
        mismatch = parse_visible_facts(page(listing_id="40503545"), "40503544")
        self.assertIn("listing_id_mismatch", mismatch.issues)
        conflict = parse_visible_facts(page() + "<p>ID авто 40503545</p>", "40503544")
        self.assertIn("conflicting_listing_id", conflict.issues)

    def test_missing_invalid_and_conflicting_creation_dates(self):
        missing = parse_visible_facts(page().replace("Оголошення створене 01.10.2026", ""), "40503544")
        self.assertIn("missing_created_date", missing.issues)
        invalid = parse_visible_facts(page(created="31.02.2026"), "40503544")
        self.assertIn("conflicting_or_invalid_created_date", invalid.issues)
        conflict = parse_visible_facts(page() + "<p>Оголошення створене 30.09.2026</p>", "40503544")
        self.assertIn("conflicting_or_invalid_created_date", conflict.issues)

    def test_adapter_joins_identity_date_active_usd_and_optional_fields(self):
        adapted = adapt_candidate(candidate(), details(), parse_visible_facts(page(), "40503544"))
        evidence = adapted.evidence
        self.assertTrue(evidence.publication_proven)
        self.assertTrue(evidence.active)
        self.assertEqual(evidence.price_usd, Decimal("10000"))
        self.assertEqual(evidence.mileage_km, 150000)
        self.assertFalse(adapted.ready_for_delivery)

    def test_old_numeric_id_is_allowed_when_fresh_date_evidence_matches(self):
        old_id = "123"
        facts = parse_visible_facts(page(listing_id=old_id), old_id)
        adapted = adapt_candidate(candidate(listing_id=old_id,
            url="https://auto.ria.com/uk/auto_test_123.html"),
            details(listing_id=old_id), facts)
        self.assertTrue(adapted.evidence.publication_proven)

    def test_creation_date_mismatch_never_proves_publication(self):
        facts = parse_visible_facts(page(created="30.09.2026"), "40503544")
        adapted = adapt_candidate(candidate(), details(), facts)
        self.assertFalse(adapted.evidence.publication_proven)
        self.assertIn("creation_date_mismatch", adapted.issues)

    def test_update_or_reprice_is_not_substituted_for_add_date(self):
        changed = adapt_candidate(candidate(preview_usd=Decimal("9000")), details(),
                                  parse_visible_facts(page(), "40503544"))
        self.assertTrue(changed.evidence.publication_proven)
        self.assertIn("preview_price_changed", changed.issues)
        wrong_basis = Candidate(candidate().listing_id, candidate().url, candidate().added_at,
                                candidate().preview_usd, evidence="public_card_update_date")
        self.assertFalse(adapt_candidate(wrong_basis, details(),
            parse_visible_facts(page(), "40503544")).evidence.publication_proven)

    def test_non_usd_or_inactive_details_fail_later_gate(self):
        facts = parse_visible_facts(page(), "40503544")
        non_usd = adapt_candidate(candidate(), details(currency="EUR"), facts)
        self.assertIn("positive_current_price_not_proven",
                      gate(non_usd.evidence, SearchFilter()).blockers)
        inactive = adapt_candidate(candidate(), details(availability="unavailable"), facts)
        self.assertIn("inactive", gate(inactive.evidence, SearchFilter()).blockers)

    def test_missing_optional_details_remain_unknown(self):
        sparse = details(year=None, body=None, fuel=None, transmission=None, mileage_km=None)
        result = gate(adapt_candidate(candidate(), sparse,
            parse_visible_facts(page(), "40503544")).evidence, SearchFilter())
        self.assertTrue(result.eligible_for_valuation)

    def test_damage_is_a_notice_not_a_blocker(self):
        damaged = details(condition="https://schema.org/DamagedCondition")
        result = gate(adapt_candidate(candidate(), damaged,
            parse_visible_facts(page(), "40503544")).evidence, SearchFilter())
        self.assertTrue(result.eligible_for_valuation)
        self.assertIn("damage", result.notices)

    def test_resource_limits_and_input_types(self):
        with self.assertRaises(VisibleParseError):
            parse_visible_facts("x" * 2_500_001, "40503544")
        with self.assertRaises(VisibleParseError):
            parse_visible_facts(page(), "bad")
        with self.assertRaises(VisibleParseError):
            adapt_candidate(object(), details(), parse_visible_facts(page(), "40503544"))

    def test_module_has_no_network_backend_or_delivery_imports(self):
        source = Path("experiments/free_search/visible_adapter.py").read_text()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names.isdisjoint({"backend", "requests", "urllib", "httpx", "sqlalchemy", "telegram"}))


if __name__ == "__main__":
    unittest.main()
