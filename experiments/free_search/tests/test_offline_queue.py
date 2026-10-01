import ast
import copy
from pathlib import Path
import unittest

from experiments.free_search.offline_queue import (
    Claim, QueueState, QueueStateError, Subscription, begin_evaluation,
    begin_send, complete_evaluation, finish_send, put_subscription, put_user,
    queue_candidate, recover_after_restart, stop_user,
)


PUB = 1_790_820_000.0


def base():
    state = put_user(QueueState(), 111, True)
    state = put_user(state, 222, True)
    state = put_subscription(state, Subscription(1, 111, "epoch-a", PUB - 100, True))
    state = put_subscription(state, Subscription(2, 222, "epoch-b", PUB - 100, True))
    return queue_candidate(state, "40503544", PUB)


def evaluated(state=None, confirmed=True, matches=(1, 2), max_claims=512):
    state = state or base()
    state, snapshot = begin_evaluation(state, "40503544", matches)
    return complete_evaluation(state, snapshot, confirmed, max_claims=max_claims)


class OfflineQueueTests(unittest.TestCase):
    def test_one_shared_job_fans_out_to_two_users(self):
        state = evaluated()
        self.assertEqual(len(state.jobs), 1)
        self.assertEqual([(row.user_id, row.state) for row in state.claims], [(111, "pending"), (222, "pending")])

    def test_two_matching_searches_of_one_user_create_one_claim(self):
        state = put_subscription(base(), Subscription(3, 111, "epoch-c", PUB - 50, True))
        state, snapshot = begin_evaluation(state, "40503544", (1, 2, 3))
        state = complete_evaluation(state, snapshot, True)
        claims = [row for row in state.claims if row.user_id == 111]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].matches, ((1, "epoch-a"), (3, "epoch-c")))

    def test_late_join_is_not_attached_to_old_publication(self):
        state = put_user(base(), 333, True)
        state = put_subscription(state, Subscription(3, 333, "epoch-c", PUB + 1, True))
        state, snapshot = begin_evaluation(state, "40503544", (1, 2, 3))
        state = complete_evaluation(state, snapshot, True)
        self.assertNotIn(333, {row.user_id for row in state.claims})

    def test_epoch_change_while_evaluation_is_in_flight_blocks_old_snapshot(self):
        state, snapshot = begin_evaluation(base(), "40503544", (1, 2))
        state = put_subscription(state, Subscription(2, 222, "epoch-new", PUB - 10, True))
        state = complete_evaluation(state, snapshot, True)
        self.assertEqual([row.user_id for row in state.claims], [111])

    def test_stop_while_evaluation_is_in_flight_blocks_user(self):
        state, snapshot = begin_evaluation(base(), "40503544", (1, 2))
        state = stop_user(state, 222)
        state = complete_evaluation(state, snapshot, True)
        self.assertEqual([row.user_id for row in state.claims], [111])
        self.assertFalse(next(row for row in state.users if row.user_id == 222).ready)

    def test_unconfirmed_valuation_creates_no_delivery_claim(self):
        state = evaluated(confirmed=False)
        self.assertEqual(state.claims, ())
        self.assertEqual(state.jobs[0].state, "unvalued")
        self.assertEqual({row.state for row in state.seen}, {"unvalued"})

    def test_stop_cancels_pending_but_preserves_other_claim_states(self):
        state = evaluated()
        claims = tuple(Claim(row.user_id, row.listing_id, row.matches,
                             "sending" if row.user_id == 222 else row.state) for row in state.claims)
        state = stop_user(QueueState(state.users, state.subscriptions, state.jobs, state.seen, claims), 111)
        self.assertEqual(next(row for row in state.claims if row.user_id == 111).state, "cancelled")
        self.assertEqual(next(row for row in state.claims if row.user_id == 222).state, "sending")

    def test_begin_send_rechecks_stop_and_epoch(self):
        stopped = stop_user(evaluated(), 111)
        with self.assertRaises(QueueStateError):
            begin_send(stopped, 111, "40503544")
        changed = evaluated()
        changed = put_subscription(changed, Subscription(1, 111, "epoch-new", PUB + 1, True))
        changed, allowed = begin_send(changed, 111, "40503544")
        self.assertFalse(allowed)
        self.assertEqual(next(row for row in changed.claims if row.user_id == 111).state, "cancelled")

    def test_send_claim_is_persisted_before_hypothetical_io(self):
        state, allowed = begin_send(evaluated(), 111, "40503544")
        self.assertTrue(allowed)
        self.assertEqual(next(row for row in state.claims if row.user_id == 111).state, "sending")

    def test_send_outcomes_are_explicit(self):
        expected = {"accepted": "sent", "ambiguous": "uncertain", "rejected": "failed", "rate_limited": "pending"}
        for outcome, final in expected.items():
            with self.subTest(outcome=outcome):
                state, _ = begin_send(evaluated(), 111, "40503544")
                state = finish_send(state, 111, "40503544", outcome)
                self.assertEqual(next(row for row in state.claims if row.user_id == 111).state, final)

    def test_restart_retries_evaluation_but_never_replays_sending(self):
        state, snapshot = begin_evaluation(base(), "40503544", (1, 2))
        restored = recover_after_restart(QueueState.from_dict(state.as_dict()))
        self.assertEqual(restored.jobs[0].state, "pending")
        restored, snapshot2 = begin_evaluation(restored, "40503544", (1, 2))
        self.assertEqual(snapshot.targets, snapshot2.targets)
        restored = complete_evaluation(restored, snapshot2, True)
        restored, _ = begin_send(restored, 111, "40503544")
        restored = recover_after_restart(QueueState.from_dict(restored.as_dict()))
        self.assertEqual(next(row for row in restored.claims if row.user_id == 111).state, "uncertain")
        with self.assertRaises(QueueStateError):
            begin_send(restored, 111, "40503544")

    def test_any_existing_claim_state_is_final_dedupe_authority(self):
        for claim_state in ("pending", "sending", "sent", "uncertain", "failed", "cancelled"):
            with self.subTest(claim_state=claim_state):
                state = base()
                claim = Claim(111, "40503544", ((1, "epoch-a"),), claim_state)
                state = QueueState(state.users, state.subscriptions, state.jobs, (), (claim,))
                state, snapshot = begin_evaluation(state, "40503544", (1, 2))
                state = complete_evaluation(state, snapshot, True)
                self.assertEqual(len([row for row in state.claims if row.user_id == 111]), 1)

    def test_stopped_state_survives_restart_and_is_not_auto_reenabled(self):
        stopped = stop_user(base(), 111)
        restored = QueueState.from_dict(copy.deepcopy(stopped.as_dict()))
        self.assertFalse(next(row for row in restored.users if row.user_id == 111).ready)
        self.assertFalse(next(row for row in restored.subscriptions if row.search_id == 1).enabled)

    def test_same_or_older_candidate_deduplicates_but_genuine_later_date_requeues(self):
        state = base()
        self.assertEqual(queue_candidate(state, "40503544", PUB), state)
        self.assertEqual(queue_candidate(state, "40503544", PUB - 1), state)
        done = evaluated(state)
        later = queue_candidate(done, "40503544", PUB + 3600)
        self.assertEqual(later.jobs[0].publication_at, PUB + 3600)
        self.assertEqual(later.jobs[0].state, "pending")

    def test_claim_prevents_realert_after_genuine_later_publication(self):
        state = evaluated()
        state = queue_candidate(state, "40503544", PUB + 3600)
        state, snapshot = begin_evaluation(state, "40503544", (1, 2))
        state = complete_evaluation(state, snapshot, True)
        self.assertEqual(len(state.claims), 2)

    def test_job_queue_is_bounded_and_counts_overflow(self):
        state = QueueState()
        for index in range(1, 4):
            state = queue_candidate(state, str(index), PUB + index, max_jobs=2)
        self.assertEqual(len(state.jobs), 2)
        self.assertEqual(state.job_overflow, 1)

    def test_claim_queue_is_bounded_and_counts_each_skipped_user(self):
        state = evaluated(max_claims=1)
        self.assertEqual(len(state.claims), 1)
        self.assertEqual(state.claim_overflow, 1)

    def test_state_roundtrip_is_canonical_and_rejects_corruption(self):
        state = evaluated()
        self.assertEqual(QueueState.from_dict(state.as_dict()), state)
        broken = state.as_dict()
        broken["claims"][0]["state"] = "invented"
        with self.assertRaises(QueueStateError):
            QueueState.from_dict(broken)
        extra = state.as_dict()
        extra["secret"] = True
        with self.assertRaises(QueueStateError):
            QueueState.from_dict(extra)
        malformed_match = state.as_dict()
        malformed_match["claims"][0]["matches"] = [[1]]
        with self.assertRaises(QueueStateError):
            QueueState.from_dict(malformed_match)

    def test_module_has_no_network_database_backend_or_telegram_imports(self):
        source = Path("experiments/free_search/offline_queue.py").read_text()
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
