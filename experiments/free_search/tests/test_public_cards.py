import ast
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest

from experiments.free_search.public_cards import (
    CardParseError, PublicationState, advance_publications, parse_public_cards, source_time,
)

ID = "12345678"
URL = "https://auto.ria.com/uk/auto_example_model_12345678.html"


def card(sid=ID, added="2026-10-01 02:00:00", updated="2026-10-01 02:01:00",
         price="5300", extra="", url=None):
    link = url or URL.replace(ID, sid)
    update = "" if updated is None else f'<span data-update-date="{updated}"></span>'
    price_html = "" if price is None else f'<div class="price-ticket" data-main-currency="USD" data-main-price="{price}"></div>'
    return (f'<section class="ticket-item" data-advertisement-id="{sid}"><div data-add-date="{added}">'
            f'<a class="m-link-ticket" href="{link}"></a>{update}{price_html}{extra}</div></section>')


def ts(text):
    return source_time(text)


class CardTests(unittest.TestCase):
    def test_valid_card_is_exact_and_scoped(self):
        out = parse_public_cards(card())[0]
        self.assertEqual((out.listing_id, out.url, out.preview_usd), (ID, URL, Decimal("5300")))
        self.assertEqual(out.issues, ())
        self.assertNotEqual(out.added_at, out.updated_at)

    def test_child_sections_remain_in_card_scope(self):
        html = (f'<section class="ticket-item" data-advertisement-id="{ID}"><section><i data-add-date="2026-10-01 02:00:00"></i>'
                f'<a class="m-link-ticket" href="{URL}"></a></section></section>')
        self.assertEqual(parse_public_cards(html)[0].listing_id, ID)

    def test_promotions_are_flagged_by_class_or_attribute(self):
        variants = ['<b class="paid"></b>', '<b data-sponsored="true"></b>', '<b data-is-advert="1"></b>', '<b class="ticket-item--paid"></b>']
        for extra in variants:
            with self.subTest(extra=extra):
                c = parse_public_cards(card(extra=extra))[0]
                self.assertTrue(c.promoted); self.assertIn("promoted", c.issues)

    def test_bad_add_date_cannot_be_publication_proof(self):
        for added in ("", "not-a-date", "2026-10-01", "2026-03-29 03:30:00"):
            with self.subTest(added=added):
                c = parse_public_cards(card(added=added))[0]
                self.assertIsNone(c.added_at); self.assertIn("missing_or_invalid_add_date", c.issues)

    def test_explicit_utc_timestamp(self):
        c = parse_public_cards(card(added="2026-09-30T23:00:00Z"))[0]
        self.assertEqual(c.added_at, datetime(2026, 9, 30, 23, tzinfo=timezone.utc).timestamp())

    def test_update_date_is_separate(self):
        c = parse_public_cards(card(added="2026-10-01 01:00:00", updated="2026-10-01 02:00:00"))[0]
        self.assertEqual(c.added_at, ts("2026-10-01 01:00:00"))
        self.assertEqual(c.updated_at, ts("2026-10-01 02:00:00"))

    def test_missing_update_is_allowed_but_not_evidence(self):
        c = parse_public_cards(card(updated=None))[0]
        self.assertIsNone(c.updated_at); self.assertEqual(c.issues, ())

    def test_bad_or_duplicate_links_are_flagged(self):
        bad = ("http://auto.ria.com/uk/auto_example_model_12345678.html", URL + "?x=1",
               URL.replace("auto.ria.com", "evil.invalid"), URL.replace(ID, "98765432"))
        for url in bad:
            with self.subTest(url=url):
                self.assertIn("missing_or_invalid_url", parse_public_cards(card(url=url))[0].issues)
        double = card(extra=f'<a class="m-link-ticket" href="{URL}"></a>')
        self.assertIn("missing_or_invalid_url", parse_public_cards(double)[0].issues)

    def test_preview_price_is_optional_non_authoritative(self):
        self.assertIsNone(parse_public_cards(card(price=None))[0].preview_usd)
        for price in ("0", "-1", "NaN", "5300.123", "1e3"):
            with self.subTest(price=price):
                c = parse_public_cards(card(price=price))[0]
                self.assertIn("invalid_or_conflicting_preview_price", c.issues)

    def test_duplicate_page_id_fails_closed(self):
        with self.assertRaisesRegex(CardParseError, "^invalid_or_duplicate_id$"):
            parse_public_cards(card() + card())

    def test_nested_or_unclosed_card_fails(self):
        nested = f'<section class="ticket-item" data-advertisement-id="{ID}">' + card("98765432") + '</section>'
        for body in (nested, f'<section class="ticket-item" data-advertisement-id="{ID}">'):
            with self.subTest(body=body[:30]):
                with self.assertRaises(CardParseError): parse_public_cards(body)

    def test_empty_too_many_and_oversize_fail(self):
        cases = ["", card() * 201, "x" * 2_500_001]
        for body in cases:
            with self.subTest(length=len(body)):
                with self.assertRaises(CardParseError): parse_public_cards(body)

    def test_first_snapshot_is_baseline_and_sends_nothing(self):
        cards = parse_public_cards(card())
        state, candidates = advance_publications(PublicationState(None), cards, ts("2026-10-01 02:05:00"))
        self.assertEqual(candidates, ())
        self.assertEqual(state.baseline_at, ts("2026-10-01 02:05:00"))

    def test_new_addition_after_baseline_is_candidate_once(self):
        baseline = ts("2026-10-01 01:59:00"); observed = ts("2026-10-01 02:05:00")
        state, candidates = advance_publications(PublicationState(baseline), parse_public_cards(card()), observed)
        self.assertEqual([c.listing_id for c in candidates], [ID])
        state2, candidates2 = advance_publications(state, parse_public_cards(card(updated="2026-10-01 02:04:00")), observed + 60)
        self.assertEqual(candidates2, ()); self.assertEqual(state2, state)

    def test_update_only_never_becomes_new_publication(self):
        baseline = ts("2026-10-01 01:59:00")
        first = parse_public_cards(card(added="2026-10-01 01:00:00", updated="2026-10-01 02:02:00"))
        state, candidates = advance_publications(PublicationState(baseline), first, ts("2026-10-01 02:05:00"))
        self.assertEqual(candidates, ()); self.assertEqual(state.seen_additions, ())

    def test_raise_or_preview_change_is_not_publication(self):
        baseline = ts("2026-10-01 01:59:00")
        initial = parse_public_cards(card(price="5300"))
        state, candidates = advance_publications(PublicationState(baseline), initial, ts("2026-10-01 02:05:00"))
        self.assertEqual(len(candidates), 1)
        changed = parse_public_cards(card(price="6000", updated="2026-10-01 02:06:00"))
        _, candidates = advance_publications(state, changed, ts("2026-10-01 02:07:00"))
        self.assertEqual(candidates, ())

    def test_old_id_genuine_new_add_date_is_candidate(self):
        old = ts("2026-09-30 22:00:00"); new = ts("2026-10-01 02:00:00")
        state = PublicationState(ts("2026-10-01 01:59:00"), ((ID, old),))
        next_state, candidates = advance_publications(state, parse_public_cards(card()), ts("2026-10-01 02:05:00"))
        self.assertEqual(len(candidates), 1); self.assertEqual(candidates[0].added_at, new)
        self.assertEqual(dict(next_state.seen_additions)[ID], new)

    def test_backdated_reappearance_is_not_candidate(self):
        current = ts("2026-10-01 02:00:00")
        state = PublicationState(ts("2026-10-01 01:30:00"), ((ID, current),))
        _, candidates = advance_publications(state, parse_public_cards(card(added="2026-10-01 01:50:00")), ts("2026-10-01 02:05:00"))
        self.assertEqual(candidates, ())

    def test_future_stale_promoted_and_invalid_cards_are_skipped(self):
        baseline = ts("2026-10-01 01:00:00"); observed = ts("2026-10-01 02:05:00")
        bodies = [card(added="2026-10-01 02:06:00"), card(added="2026-10-01 00:00:00"), card(extra='<b class="paid"></b>'), card(added="bad")]
        for body in bodies:
            with self.subTest(body=body[-50:]):
                _, candidates = advance_publications(PublicationState(baseline), parse_public_cards(body), observed)
                self.assertEqual(candidates, ())

    def test_state_roundtrip_restart_deduplicates(self):
        baseline = ts("2026-10-01 01:59:00"); observed = ts("2026-10-01 02:05:00")
        state, candidates = advance_publications(PublicationState(baseline), parse_public_cards(card()), observed)
        self.assertEqual(len(candidates), 1)
        restored = PublicationState.from_dict(state.as_dict())
        self.assertEqual(restored, state)
        _, repeated = advance_publications(restored, parse_public_cards(card()), observed + 30)
        self.assertEqual(repeated, ())

    def test_invalid_state_and_clock_regression_fail(self):
        for value in ({"baseline_at": "bad", "seen_additions": {}}, {"baseline_at": 1.0, "seen_additions": {"bad": 2.0}}, {"baseline_at": 1.0, "seen_additions": []}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CardParseError, "^invalid_state$"): PublicationState.from_dict(value)
        with self.assertRaisesRegex(CardParseError, "^clock_regression$"):
            advance_publications(PublicationState(100.0), (), 99.0)

    def test_candidate_never_claims_delivery_or_valuation(self):
        state, candidates = advance_publications(PublicationState(ts("2026-10-01 01:59:00")), parse_public_cards(card()), ts("2026-10-01 02:05:00"))
        self.assertEqual(candidates[0].evidence, "public_card_add_date")
        self.assertFalse(hasattr(candidates[0], "delivery")); self.assertFalse(hasattr(candidates[0], "valuation"))

    def test_no_network_or_production_dependencies(self):
        path = Path(__file__).parents[1] / "public_cards.py"
        tree = ast.parse(path.read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(modules, {"dataclasses", "decimal", "datetime", "html.parser", "math", "re", "urllib.parse", "zoneinfo"})


if __name__ == "__main__":
    unittest.main()
