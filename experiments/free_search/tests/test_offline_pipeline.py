import ast
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from experiments.free_search.filter_gate import SearchFilter, Span
from experiments.free_search.http_budget import HttpPolicy, SourcePermission
from experiments.free_search.offline_pipeline import (
    PipelineState, complete_detail_fixture, complete_research_estimate,
    ingest_feed_fixture, recover_pipeline_after_restart, reserve_detail,
)
from experiments.free_search.offline_queue import (
    QueueState, Subscription, put_subscription, put_user, stop_user,
)


KYIV = ZoneInfo("Europe/Kyiv")
BASELINE = datetime(2026, 10, 1, 7, 0, tzinfo=KYIV)
ADDED = BASELINE + timedelta(seconds=30)
OBSERVED = BASELINE + timedelta(seconds=60)
ID = "40503544"
URL = f"https://auto.ria.com/uk/auto_fixture_{ID}.html"
ALLOWED = SourcePermission(True, True, True)  # synthetic test only; live terms gate remains false
DENIED = SourcePermission(False, True, True)
POLICY = HttpPolicy(8, 2, 20, 100, 200_000, 1_000_000, 20_000, 3, 10, 30)


def card(listing_id, added, price="5000", updated=None):
    url = f"https://auto.ria.com/uk/auto_fixture_{listing_id}.html"
    update = "" if updated is None else f' data-update-date="{updated.isoformat()}"'
    return f'''<section class="ticket-item" data-advertisement-id="{listing_id}">
      <div data-add-date="{added.isoformat()}"{update}>
        <a class="m-link-ticket" href="{url}">fixture</a>
        <span class="price-ticket" data-main-currency="USD" data-main-price="{price}"></span>
      </div></section>'''


def page(price="5000", body="Універсал", availability="https://schema.org/InStock"):
    vehicle = {
        "@type": "Vehicle", "@id": URL, "url": URL,
        "brand": {"@type": "Brand", "name": "Fixture"}, "model": "Shared",
        "productionDate": "2015", "fuelType": "Дизель", "bodyType": body,
        "vehicleTransmission": "Автомат",
        "mileageFromOdometer": {"value": 150000, "unitCode": "KMT"},
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD",
                   "availability": availability},
    }
    payload = json.dumps(vehicle, ensure_ascii=False)
    return f'''<!doctype html><html><head><link rel="canonical" href="{URL}">
      <script type="application/ld+json">{payload}</script></head><body>
      <a>Легкові з пробігом</a><a>Тернопільська область</a><h1>Fixture</h1>
      <p>Оголошення створене 01.10.2026</p><p>ID авто {ID}</p></body></html>'''


def queue_state():
    state = QueueState()
    for user_id in (111, 222, 333, 444):
        state = put_user(state, user_id, True)
    for search_id, user_id in ((1, 111), (2, 222), (3, 333), (4, 444)):
        state = put_subscription(state, Subscription(
            search_id, user_id, f"epoch-{search_id}", BASELINE.timestamp() - 100, True))
    return state


def bindings():
    common = dict(regions=frozenset({"Тернопільська область"}),
                  price_usd=Span(maximum=Decimal("6000")),
                  bodies=frozenset({"Універсал"}))
    return {
        1: SearchFilter(**common, min_discount_percent=Decimal("15")),
        2: SearchFilter(**common, min_discount_percent=Decimal("20")),
        3: SearchFilter(**common, min_discount_percent=Decimal("30")),
        4: SearchFilter(**{**common, "bodies": frozenset({"Седан"})},
                        min_discount_percent=Decimal("10")),
    }


def discovered(policy=POLICY):
    state = PipelineState(queue=queue_state())
    state, baseline = ingest_feed_fixture(
        state, card("40500000", BASELINE - timedelta(seconds=10)), BASELINE.timestamp(), policy)
    assert baseline.baseline_only
    state, result = ingest_feed_fixture(state, card(ID, ADDED), OBSERVED.timestamp(), policy)
    assert result.queued == 1
    return state


def detailed():
    state = discovered()
    state, reservation = reserve_detail(state, OBSERVED.timestamp() + 1, ALLOWED, POLICY)
    state, result = complete_detail_fixture(
        state, reservation.reservation_id, OBSERVED.timestamp() + 2, page(), bindings(), POLICY)
    assert result.processed
    return state


class OfflinePipelineTests(unittest.TestCase):
    def test_one_detail_and_one_estimate_fan_out_to_multiple_users(self):
        state = discovered()
        self.assertEqual(len(state.pending_details), 1)
        state, reservation = reserve_detail(state, OBSERVED.timestamp() + 1, ALLOWED, POLICY)
        self.assertTrue(reservation.allowed)
        state, detail = complete_detail_fixture(
            state, reservation.reservation_id, OBSERVED.timestamp() + 2,
            page(), bindings(), POLICY)
        self.assertEqual(detail.matching_search_ids, (1, 2, 3))
        self.assertEqual(len(state.http.ledger), 1)
        state, research = complete_research_estimate(
            state, ID, Decimal("7000"), True, bindings())
        self.assertEqual(research.qualifying_search_ids, (1, 2))
        self.assertEqual(research.claims_created, 2)
        self.assertEqual(len(state.queue.jobs), 1)
        self.assertEqual({row.user_id for row in state.queue.claims}, {111, 222})
        self.assertEqual(state.research_candidates, ())

    def test_min_discount_and_known_filter_conflict_are_both_respected(self):
        state = detailed()
        self.assertEqual(state.research_candidates[0].matching_search_ids, (1, 2, 3))
        state, result = complete_research_estimate(
            state, ID, Decimal("7000"), True, bindings())
        self.assertNotIn(3, result.qualifying_search_ids)  # 28.57% is below 30%
        self.assertNotIn(4, {row.search_id for row in state.queue.seen})  # known body conflict

    def test_missing_optional_fields_do_not_hide_candidate(self):
        html = page().replace('"fuelType": "Дизель", ', '').replace(
            '"vehicleTransmission": "Автомат", ', '')
        state = discovered()
        state, reservation = reserve_detail(state, OBSERVED.timestamp() + 1, ALLOWED, POLICY)
        state, result = complete_detail_fixture(
            state, reservation.reservation_id, OBSERVED.timestamp() + 2,
            html, bindings(), POLICY)
        self.assertTrue(result.processed)
        self.assertEqual(result.matching_search_ids, (1, 2, 3))

    def test_terms_denial_creates_no_detail_evaluation_or_claim(self):
        state = discovered()
        unchanged, decision = reserve_detail(state, OBSERVED.timestamp() + 1, DENIED, POLICY)
        self.assertEqual(decision.reason, "terms_unapproved")
        self.assertEqual(unchanged, state)
        self.assertEqual(state.research_candidates, ())
        self.assertEqual(state.queue.jobs, ())
        self.assertEqual(state.queue.claims, ())

    def test_429_backoff_creates_no_research_or_claim(self):
        state = discovered()
        state, reservation = reserve_detail(state, OBSERVED.timestamp() + 1, ALLOWED, POLICY)
        state, result = complete_detail_fixture(
            state, reservation.reservation_id, OBSERVED.timestamp() + 2,
            "", bindings(), POLICY, status=429, retry_after_seconds=120)
        self.assertFalse(result.processed)
        self.assertEqual(result.reason, "rate_limited")
        self.assertEqual(state.research_candidates, ())
        self.assertEqual(state.queue.claims, ())
        state, decision = reserve_detail(state, OBSERVED.timestamp() + 100, ALLOWED, POLICY)
        self.assertEqual(decision.reason, "source_backoff")

    def test_stop_after_detail_is_rechecked_before_claim_creation(self):
        state = detailed()
        state = PipelineState(state.publications, state.http, stop_user(state.queue, 222),
                              state.pending_details, state.research_candidates)
        state, result = complete_research_estimate(
            state, ID, Decimal("7000"), True, bindings())
        self.assertEqual(result.qualifying_search_ids, (1, 2))
        self.assertEqual({row.user_id for row in state.queue.claims}, {111})

    def test_unconfirmed_research_never_creates_claims(self):
        state = detailed()
        state, result = complete_research_estimate(
            state, ID, Decimal("7000"), False, bindings())
        self.assertEqual(result.reason, "unconfirmed")
        self.assertEqual(state.queue.claims, ())
        self.assertEqual(state.queue.jobs[0].state, "unvalued")

    def test_restart_preserves_pending_candidate_and_reserved_budget(self):
        state = discovered()
        state, _ = reserve_detail(state, OBSERVED.timestamp() + 1, ALLOWED, POLICY)
        restored = PipelineState.from_dict(state.as_dict())
        restored = recover_pipeline_after_restart(restored, OBSERVED.timestamp() + 10, POLICY)
        self.assertEqual(len(restored.pending_details), 1)
        self.assertEqual(restored.http.works[0].state, "queued")
        self.assertEqual(restored.http.ledger[0].charged_bytes, POLICY.max_body_bytes)
        self.assertTrue(restored.http.ledger[0].settled)
        restored, decision = reserve_detail(
            restored, OBSERVED.timestamp() + 39, ALLOWED, POLICY)
        self.assertEqual(decision.reason, "nothing_due")

    def test_restart_preserves_research_candidate_and_stopped_user(self):
        state = detailed()
        stopped = stop_user(state.queue, 222)
        state = PipelineState(state.publications, state.http, stopped,
                              state.pending_details, state.research_candidates)
        restored = PipelineState.from_dict(state.as_dict())
        restored, _ = complete_research_estimate(
            restored, ID, Decimal("7000"), True, bindings())
        self.assertEqual({row.user_id for row in restored.queue.claims}, {111})
        self.assertFalse(next(row for row in restored.queue.users if row.user_id == 222).ready)

    def test_queue_overflow_rolls_back_publication_progress(self):
        tiny = HttpPolicy(1, 1, 20, 100, 200_000, 1_000_000, 20_000, 3, 10, 30)
        state = PipelineState(queue=queue_state())
        state, _ = ingest_feed_fixture(
            state, card("40500000", BASELINE - timedelta(seconds=10)), BASELINE.timestamp(), tiny)
        html = card(ID, ADDED) + card("40503545", ADDED + timedelta(seconds=1))
        original_publications = state.publications
        state, result = ingest_feed_fixture(state, html, OBSERVED.timestamp(), tiny)
        self.assertEqual(result.reason, "detail_queue_full")
        self.assertEqual(state.publications, original_publications)
        self.assertEqual(state.pending_details, ())
        self.assertEqual(state.http.works, ())
        self.assertEqual(state.http.queue_overflow, 1)

    def test_same_or_update_only_card_does_not_requeue(self):
        state = discovered()
        before = state
        state, result = ingest_feed_fixture(
            state, card(ID, ADDED, price="4500", updated=OBSERVED + timedelta(minutes=5)),
            (OBSERVED + timedelta(minutes=5)).timestamp(), POLICY)
        self.assertEqual(result.reason, "no_candidates")
        self.assertEqual(state.pending_details, before.pending_details)
        self.assertEqual(len(state.http.works), 1)

    def test_genuine_later_old_id_is_rechecked_but_never_realerted(self):
        state = detailed()
        state, _ = complete_research_estimate(state, ID, Decimal("7000"), True, bindings())
        self.assertEqual(len(state.queue.claims), 2)
        later_add = ADDED + timedelta(minutes=30)
        later_observed = later_add + timedelta(seconds=30)
        state, result = ingest_feed_fixture(
            state, card(ID, later_add), later_observed.timestamp(), POLICY)
        self.assertEqual(result.queued, 1)
        state, reservation = reserve_detail(
            state, later_observed.timestamp() + 1, ALLOWED, POLICY)
        state, _ = complete_detail_fixture(
            state, reservation.reservation_id, later_observed.timestamp() + 2,
            page(), bindings(), POLICY)
        state, result = complete_research_estimate(
            state, ID, Decimal("7000"), True, bindings())
        self.assertEqual(result.claims_created, 0)
        self.assertEqual(len(state.queue.claims), 2)

    def test_state_rejects_extra_or_private_fields(self):
        raw = discovered().as_dict()
        raw["raw_html"] = "forbidden"
        with self.assertRaisesRegex(Exception, "invalid_state"):
            PipelineState.from_dict(raw)

    def test_module_has_no_network_backend_database_ai_or_telegram_imports(self):
        source = Path("experiments/free_search/offline_pipeline.py").read_text()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names.isdisjoint({"backend", "requests", "urllib", "httpx",
                                          "socket", "sqlalchemy", "telegram", "openai"}))


if __name__ == "__main__":
    unittest.main()
