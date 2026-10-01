import ast
import copy
from dataclasses import replace
from pathlib import Path
import unittest

from experiments.free_search.http_budget import (
    HttpBudgetError, HttpPolicy, HttpState, SourcePermission, enqueue,
    record_response, record_transport_error, recover_after_restart, reserve_next,
)


NOW = 1_790_823_600.0
ALLOWED = SourcePermission(True, True, True)
POLICY = HttpPolicy(
    max_queue=4,
    max_inflight=2,
    hourly_requests=4,
    daily_requests=8,
    hourly_bytes=4000,
    daily_bytes=8000,
    max_body_bytes=1000,
    max_attempts=3,
    base_backoff_seconds=10,
    restart_backoff_seconds=30,
)


def queued(count=1, policy=POLICY):
    state = HttpState()
    for number in range(1, count + 1):
        state, added = enqueue(state, f"feed:{number}",
                               "feed_page_1" if number % 2 else "feed_page_2",
                               NOW + number, policy)
        assert added
    return state


def reserve(state, now=NOW + 10, permission=ALLOWED, policy=POLICY):
    return reserve_next(state, now, permission, policy)


class HttpBudgetTests(unittest.TestCase):
    def test_shared_key_is_deduplicated(self):
        state, added = enqueue(HttpState(), "detail:40503544", "detail", NOW, POLICY)
        self.assertTrue(added)
        same, added = enqueue(state, "detail:40503544", "detail", NOW + 1, POLICY)
        self.assertFalse(added)
        self.assertEqual(same, state)

    def test_queue_is_bounded_and_overflow_is_counted(self):
        policy = HttpPolicy(1, 1, 4, 8, 4000, 8000, 1000, 3, 10, 30)
        state = queued(1, policy)
        state, added = enqueue(state, "feed:2", "feed_page_2", NOW + 2, policy)
        self.assertFalse(added)
        self.assertEqual(state.queue_overflow, 1)

    def test_every_permission_gate_must_pass(self):
        reasons = (
            (SourcePermission(False, True, True), "terms_unapproved"),
            (SourcePermission(True, False, True), "robots_disallowed"),
            (SourcePermission(True, True, False), "path_not_approved"),
        )
        for permission, reason in reasons:
            with self.subTest(reason=reason):
                state = queued()
                unchanged, decision = reserve(state, permission=permission)
                self.assertEqual(unchanged, state)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_reservation_precedes_io_and_forbids_redirects_and_cookies(self):
        state, decision = reserve(queued())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.method, "GET")
        self.assertFalse(decision.allow_redirects)
        self.assertFalse(decision.cookies_allowed)
        self.assertEqual(decision.max_body_bytes, 1000)
        self.assertEqual(state.works[0].state, "reserved")
        self.assertEqual(state.ledger[0].charged_bytes, 1000)
        self.assertFalse(state.ledger[0].settled)

    def test_inflight_limit_is_enforced(self):
        policy = HttpPolicy(4, 1, 4, 8, 4000, 8000, 1000, 3, 10, 30)
        state, first = reserve(queued(2, policy), policy=policy)
        state, second = reserve(state, policy=policy)
        self.assertTrue(first.allowed)
        self.assertEqual(second.reason, "inflight_limit")

    def test_hourly_and_daily_request_budgets_are_rolling(self):
        hourly = HttpPolicy(4, 2, 1, 8, 4000, 8000, 1000, 3, 10, 30)
        state, decision = reserve(queued(2, hourly), policy=hourly)
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 100,
                                "text/html", None, hourly)
        state, blocked = reserve(state, now=NOW + 20, policy=hourly)
        self.assertEqual(blocked.reason, "hourly_request_budget")
        state, allowed = reserve(state, now=NOW + 3611, policy=hourly)
        self.assertTrue(allowed.allowed)

        daily = HttpPolicy(4, 2, 1, 1, 4000, 8000, 1000, 3, 10, 30)
        state, decision = reserve(queued(2, daily), policy=daily)
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 100,
                                "text/html", None, daily)
        state, blocked = reserve(state, now=NOW + 3611, policy=daily)
        self.assertEqual(blocked.reason, "daily_request_budget")

    def test_hourly_and_daily_byte_budgets_reserve_worst_case(self):
        hourly = HttpPolicy(4, 2, 4, 8, 1000, 8000, 1000, 3, 10, 30)
        state, decision = reserve(queued(2, hourly), policy=hourly)
        self.assertTrue(decision.allowed)
        state, blocked = reserve(state, policy=hourly)
        self.assertEqual(blocked.reason, "hourly_byte_budget")

        daily = HttpPolicy(4, 2, 4, 8, 1000, 1000, 1000, 3, 10, 30)
        state, decision = reserve(queued(2, daily), policy=daily)
        self.assertTrue(decision.allowed)
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 1000,
                                "text/html", None, daily)
        state, blocked = reserve(state, now=NOW + 3611, policy=daily)
        self.assertEqual(blocked.reason, "daily_byte_budget")

    def test_small_html_response_settles_actual_bytes(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 321,
                                "text/html; charset=utf-8", None, POLICY)
        self.assertEqual(state.works[0].state, "complete")
        self.assertEqual(state.works[0].last_result, "accepted")
        self.assertEqual(state.ledger[0].charged_bytes, 321)
        self.assertTrue(state.ledger[0].settled)

    def test_oversized_response_is_aborted_and_bounded(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 1001,
                                "text/html", None, POLICY)
        self.assertEqual(state.works[0].state, "queued")
        self.assertEqual(state.works[0].last_result, "body_too_large")
        self.assertEqual(state.ledger[0].charged_bytes, 1000)
        self.assertEqual(state.works[0].available_at, NOW + 21)

    def test_429_honors_retry_after_and_global_pause(self):
        state, decision = reserve(queued(2))
        state = record_response(state, decision.reservation_id, NOW + 11, 429, 50,
                                "text/html", 90, POLICY)
        self.assertEqual(state.paused_until, NOW + 101)
        state, blocked = reserve(state, now=NOW + 100)
        self.assertEqual(blocked.reason, "source_backoff")
        state, allowed = reserve(state, now=NOW + 101)
        self.assertTrue(allowed.allowed)

    def test_invalid_retry_after_fails_safe_for_one_day(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 429, 0,
                                "text/html", -1, POLICY)
        self.assertEqual(state.paused_until, NOW + 11 + 86_400)

    def test_denial_blocks_source_without_bypass(self):
        state, decision = reserve(queued(2))
        state = record_response(state, decision.reservation_id, NOW + 11, 403, 20,
                                "text/html", None, POLICY)
        self.assertEqual(state.source_blocked, "http_denied")
        state, blocked = reserve(state, now=NOW + 100000)
        self.assertEqual(blocked.reason, "http_denied")

    def test_redirect_is_not_followed(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 302, 0,
                                "text/html", None, POLICY)
        self.assertEqual(state.works[0].state, "blocked")
        self.assertEqual(state.works[0].last_result, "redirect_denied")
        self.assertEqual(state.paused_until, NOW + 11 + 86_400)

    def test_not_found_is_terminal_not_retried(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 410, 100,
                                "text/html", None, POLICY)
        self.assertEqual(state.works[0].state, "unavailable")

    def test_transient_errors_back_off_exponentially_then_exhaust(self):
        state = queued()
        expected_delays = (10, 20)
        now = NOW + 10
        for delay in expected_delays:
            state, decision = reserve(state, now=now)
            state = record_response(state, decision.reservation_id, now + 1, 503, 10,
                                    "text/html", None, POLICY)
            self.assertEqual(state.works[0].available_at, now + 1 + delay)
            now = state.works[0].available_at
        state, decision = reserve(state, now=now)
        state = record_transport_error(state, decision.reservation_id, now + 1, POLICY)
        self.assertEqual(state.works[0].state, "exhausted")

    def test_unexpected_content_is_not_accepted(self):
        state, decision = reserve(queued())
        state = record_response(state, decision.reservation_id, NOW + 11, 200, 100,
                                "application/json", None, POLICY)
        self.assertEqual(state.works[0].state, "queued")
        self.assertEqual(state.works[0].last_result, "unexpected_content")

    def test_restart_keeps_full_charge_and_delays_retry(self):
        state, decision = reserve(queued())
        restored = HttpState.from_dict(copy.deepcopy(state.as_dict()))
        restored = recover_after_restart(restored, NOW + 20, POLICY)
        self.assertEqual(restored.works[0].state, "queued")
        self.assertEqual(restored.works[0].attempts, 1)
        self.assertEqual(restored.works[0].available_at, NOW + 50)
        self.assertEqual(restored.ledger[0].charged_bytes, 1000)
        self.assertTrue(restored.ledger[0].settled)
        restored, blocked = reserve(restored, now=NOW + 49)
        self.assertEqual(blocked.reason, "nothing_due")

    def test_fifo_skips_not_yet_due_work(self):
        state = queued(2)
        works = tuple(replace(row, available_at=NOW + 100) if row.key == "feed:1" else row
                      for row in state.works)
        state = HttpState(works=works, ledger=state.ledger,
                          next_sequence=state.next_sequence, next_reservation=state.next_reservation)
        state, decision = reserve(state, now=NOW + 10)
        self.assertEqual(decision.key, "feed:2")

    def test_attempt_limit_cannot_be_bypassed_by_restored_state(self):
        state = queued()
        works = (replace(state.works[0], attempts=POLICY.max_attempts),)
        state = HttpState(works=works, next_sequence=state.next_sequence)
        unchanged, decision = reserve(state)
        self.assertEqual(unchanged, state)
        self.assertEqual(decision.reason, "nothing_due")

    def test_state_roundtrip_is_strict(self):
        state, _ = reserve(queued())
        self.assertEqual(HttpState.from_dict(state.as_dict()), state)
        extra = state.as_dict()
        extra["secret"] = True
        with self.assertRaises(HttpBudgetError):
            HttpState.from_dict(extra)
        broken = state.as_dict()
        broken["ledger"][0]["charged_bytes"] = -1
        with self.assertRaises(HttpBudgetError):
            HttpState.from_dict(broken)

    def test_module_has_no_network_backend_database_or_telegram_imports(self):
        source = Path("experiments/free_search/http_budget.py").read_text()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names.isdisjoint({"backend", "requests", "urllib", "httpx",
                                          "socket", "sqlalchemy", "telegram"}))


if __name__ == "__main__":
    unittest.main()
