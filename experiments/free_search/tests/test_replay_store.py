from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from experiments.free_search.replay_store import ReplayStore
from experiments.free_search.replay_demo import run, card, BASE, POLICY
from experiments.free_search.offline_pipeline import PipelineState, PipelineError, ingest_feed_pages_fixture
from experiments.free_search.public_cards import PublicationState
from experiments.free_search.tests.test_offline_pipeline import discovered, page, bindings, ALLOWED, OBSERVED, POLICY as SMALL
from experiments.free_search.offline_pipeline import reserve_detail, complete_detail_fixture


class ReplayTests(unittest.TestCase):
    def test_atomic_rollback_and_real_file_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.free-search.sqlite3'
            store = ReplayStore(path)
            store.apply(BASE, 'baseline', lambda s: (replace(s, publications=PublicationState(BASE)), 'baseline'))
            def failed(s):
                raise RuntimeError('simulated_crash')
            with self.assertRaises(RuntimeError):
                store.apply(BASE+1, 'discovery', failed)
            store.close()
            store = ReplayStore(path)
            self.assertEqual(store.load().publications.baseline_at, BASE)
            self.assertEqual(store.diagnostics(), [{'stage':'baseline','reason':'baseline','count':1}])
            store.close()

    def test_rejects_foreign_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'foreign.free-search.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE valuable (value TEXT)')
            db.commit(); db.close()
            with self.assertRaisesRegex(ValueError, 'foreign_database'):
                ReplayStore(path)
            with self.assertRaisesRegex(ValueError, 'experiment_database_required'):
                ReplayStore(Path(folder) / 'production.db')

    def test_pagination_overlap_and_low_id_late_arrival(self):
        state = PipelineState(publications=PublicationState(BASE))
        state, result = ingest_feed_pages_fixture(state, [card('100')+card('99'), card('99')+card('2')], BASE+60, POLICY)
        self.assertEqual(result.queued, 3)
        state, result = ingest_feed_pages_fixture(state, [card('1', BASE+20)], BASE+120, POLICY)
        self.assertEqual(result.queued, 1)
        self.assertEqual(len(state.pending_details), 4)

    def test_repeated_or_conflicting_pages_fail_without_advancing(self):
        state = PipelineState(publications=PublicationState(BASE))
        for pages in ([card('100'), card('100')], [card('100'), card('100', BASE+40)+card('99')]):
            with self.assertRaises(PipelineError):
                ingest_feed_pages_fixture(state, pages, BASE+60, POLICY)
        self.assertFalse(state.pending_details)
        self.assertFalse(state.publications.seen_additions)

    def test_parse_error_retry_survives_state_roundtrip_and_is_bounded(self):
        state = discovered()
        at = OBSERVED.timestamp()+1
        state, reservation = reserve_detail(state, at, ALLOWED, SMALL)
        state, result = complete_detail_fixture(state, reservation.reservation_id, at+1, '<html>temporary</html>', bindings(), SMALL)
        self.assertEqual(len(state.pending_details), 1)
        state = PipelineState.from_dict(state.as_dict())
        state, reservation = reserve_detail(state, at+20, ALLOWED, SMALL)
        state, result = complete_detail_fixture(state, reservation.reservation_id, at+21, page(), bindings(), SMALL)
        self.assertTrue(result.processed)
        self.assertEqual(len(state.http.ledger), 2)
        state = discovered()
        for offset in (1, 30, 100):
            state, reservation = reserve_detail(state, OBSERVED.timestamp()+offset, ALLOWED, SMALL)
            state, result = complete_detail_fixture(state, reservation.reservation_id, OBSERVED.timestamp()+offset+1,
                '<html>bad</html>', bindings(), SMALL)
        self.assertFalse(state.pending_details)
        self.assertEqual(state.http.works[0].state, 'exhausted')
        self.assertEqual(state.http.works[0].last_result, 'parse_error')

    def test_simulated_multiple_users_stop_and_uncertain_delivery(self):
        result = run(5, 3)
        self.assertEqual((result['detected'], result['missed']), (120, []))
        self.assertEqual(result['claims_without_estimate'], 0)
        self.assertEqual(result['claims'], 5)
        self.assertEqual(result['mock_delivery_outcomes'], {'sent':3,'uncertain':1,'cancelled':1,'pending':0})
        self.assertEqual(result['paid_api_requests'], 0)


if __name__ == '__main__':
    unittest.main()
