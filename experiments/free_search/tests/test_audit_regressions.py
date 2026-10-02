from dataclasses import replace
from decimal import Decimal
import unittest

from experiments.free_search.filter_gate import SearchFilter, gate
from experiments.free_search.public_cards import PublicationState, advance_publications, parse_public_cards
from experiments.free_search.public_details import parse_public_details
from experiments.free_search.visible_adapter import parse_visible_facts, adapt_candidate
from experiments.free_search.offline_queue import begin_evaluation, complete_evaluation
from experiments.free_search.tests.test_offline_pipeline import (
    BASELINE, ADDED, OBSERVED, ID, card, page, discovered,
)


class AuditRegressions(unittest.TestCase):
    def evidence(self):
        item = discovered().pending_details[0].candidate
        html = page(body='Легкові')
        return adapt_candidate(item, parse_public_details(html, ID), parse_visible_facts(html, ID)).evidence

    def test_real_jsonld_category_label_is_not_a_body_style(self):
        evidence = self.evidence()
        self.assertIsNone(evidence.body)
        self.assertTrue(gate(evidence, SearchFilter(bodies=frozenset({'Універсал'}))).eligible_for_valuation)

    def test_brand_and_model_filters_are_preserved(self):
        evidence = self.evidence()
        self.assertTrue(gate(evidence, SearchFilter(brand='Fixture', model='Shared')).eligible_for_valuation)
        self.assertFalse(gate(evidence, SearchFilter(brand='Toyota')).eligible_for_valuation)
        self.assertFalse(gate(evidence, SearchFilter(model='Other')).eligible_for_valuation)
        self.assertFalse(gate(replace(evidence, brand=None), SearchFilter(brand='Fixture')).eligible_for_valuation)

    def test_corrupt_preview_and_update_do_not_hide_valid_publication(self):
        html = card(ID, ADDED, price='not-a-number').replace('data-add-date=', 'data-update-date="invalid" data-add-date=')
        cards = parse_public_cards(html)
        state, candidates = advance_publications(PublicationState(BASELINE.timestamp()), cards, OBSERVED.timestamp())
        self.assertEqual([c.listing_id for c in candidates], [ID])
        self.assertIsNone(candidates[0].preview_usd)

    def test_claim_overflow_does_not_finalize_unscheduled_recipient(self):
        from experiments.free_search.offline_queue import queue_candidate
        state = queue_candidate(discovered().queue, ID, ADDED.timestamp())
        state, snapshot = begin_evaluation(state, ID, (1, 2))
        state = complete_evaluation(state, snapshot, True, max_claims=1)
        self.assertEqual(state.jobs[0].state, 'pending')
        self.assertEqual(next(x for x in state.seen if x.search_id == 2).state, 'pending')
        state, snapshot = begin_evaluation(state, ID, (1, 2))
        state = complete_evaluation(state, snapshot, True, max_claims=2)
        self.assertEqual({x.user_id for x in state.claims}, {111, 222})

    def test_expired_publication_seen_rows_do_not_break_future_restart(self):
        old = tuple((str(100000+i), BASELINE.timestamp()-4000) for i in range(10000))
        state, candidates = advance_publications(PublicationState(BASELINE.timestamp(), old),
            parse_public_cards(card(ID, ADDED)), OBSERVED.timestamp())
        restored = PublicationState.from_dict(state.as_dict())
        self.assertEqual(len(restored.seen_additions), 1)


if __name__ == '__main__':
    unittest.main()
