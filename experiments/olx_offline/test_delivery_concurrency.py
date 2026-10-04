"""Isolated regressions for claims and fresh recipient policy; no transports."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from experiments.olx_offline.pipeline import Pipeline, filtered
from experiments.olx_offline.test_pipeline import NOW, comps, raw, users


def current(**changes):
    row = dict(source='OLX', paid=True, stopped=False, ready=True, enabled=True,
               filters={}, min_discount=15, only_deals=True)
    row.update(changes)
    return row


class DeliveryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'isolated.sqlite'
        self.pipeline = Pipeline(self.path, now=NOW)
        self.network = patch('socket.socket', side_effect=AssertionError('No network'))
        self.network.start()
        self.pipeline.collect(lambda _: {'items': [raw('car')], 'next': None},
                              NOW, page_budget=1, row_budget=10)
        self.assertEqual(self.pipeline.enqueue(users(), comps(), NOW,
                         capacity=10, olx_enabled=True)['queued'], 1)

    def tearDown(self):
        self.pipeline.db.close()
        self.network.stop()
        self.tmp.cleanup()

    def status(self):
        return self.pipeline.db.execute('SELECT status FROM deliveries').fetchone()[0]

    def deliver(self, sender=lambda *_: True, access=lambda _: True, **options):
        return self.pipeline.deliver_fake(sender, access, now=options.pop('now', NOW),
            olx_enabled=options.pop('olx_enabled', True),
            current_search=options.pop('current_search', lambda *_: current()), **options)

    def test_two_workers_one_recipient_one_sender_call(self):
        initialized = threading.Barrier(2)
        initial_access = threading.Barrier(2)
        sent, results, errors = [], [], []
        lock = threading.Lock()

        def worker():
            pipeline = Pipeline(self.path, now=NOW)
            accesses = 0
            try:
                initialized.wait(timeout=5)
                def access(_):
                    nonlocal accesses
                    accesses += 1
                    if accesses == 1:
                        initial_access.wait(timeout=5)
                    return True
                def sender(uid, payload):
                    with lock:
                        sent.append(uid)
                    return True
                results.append(pipeline.deliver_fake(sender, access, now=NOW,
                    olx_enabled=True, current_search=lambda *_: current()))
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                pipeline.db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sent, ['0'])
        self.assertEqual(sum(result['accepted'] for result in results), 1)
        self.assertEqual(self.status(), 'accepted')

    def test_second_worker_start_preserves_active_attempt(self):
        seen = []
        def sender(uid, payload):
            second = Pipeline(self.path, now=NOW + 1)
            try:
                seen.append(second.db.execute('SELECT status FROM deliveries').fetchone()[0])
                self.assertEqual(second.deliver_fake(lambda *_: self.fail('Duplicate'),
                    lambda _: True, now=NOW + 1, olx_enabled=True)['accepted'], 0)
            finally:
                second.db.close()
            return True
        self.assertEqual(self.deliver(sender)['accepted'], 1)
        self.assertEqual(seen, ['sending'])

    def test_expired_unattempted_claim_returns_pending(self):
        self.assertIsNotNone(self.pipeline._claim_delivery(('0', 'olx', 'car'), NOW))
        second = Pipeline(self.path, now=NOW + 31)
        try:
            self.assertEqual(second.db.execute('SELECT status FROM deliveries').fetchone()[0], 'pending')
            self.assertEqual(second.deliver_fake(lambda *_: True, lambda _: True,
                now=NOW + 31, olx_enabled=True, current_search=lambda *_: current())['accepted'], 1)
        finally:
            second.db.close()

    def test_expired_attempt_is_uncertain_and_never_replayed(self):
        key = ('0', 'olx', 'car')
        token = self.pipeline._claim_delivery(key, NOW)
        self.assertTrue(self.pipeline._start_delivery(key, token, NOW))
        second = Pipeline(self.path, now=NOW + 31)
        try:
            self.assertEqual(second.db.execute('SELECT status FROM deliveries').fetchone()[0], 'uncertain')
            self.assertEqual(second.deliver_fake(lambda *_: self.fail('Ambiguous replay'),
                lambda _: True, now=NOW + 31, olx_enabled=True)['accepted'], 0)
        finally:
            second.db.close()

    def test_late_ack_settles_its_own_uncertain_token(self):
        def sender(uid, payload):
            second = Pipeline(self.path, now=NOW + 31)
            try:
                self.assertEqual(second.db.execute('SELECT status FROM deliveries').fetchone()[0], 'uncertain')
            finally:
                second.db.close()
            return True
        self.assertEqual(self.deliver(sender)['accepted'], 1)
        self.assertEqual(self.status(), 'accepted')

    def test_old_token_cannot_send_reclaimed_work(self):
        key = ('0', 'olx', 'car')
        old = self.pipeline._claim_delivery(key, NOW)
        self.pipeline.recover_delivery_claims(NOW + 31)
        replacement = self.pipeline._claim_delivery(key, NOW + 31)
        self.assertNotEqual(old, replacement)
        self.assertFalse(self.pipeline._start_delivery(key, old, NOW + 31))
        self.assertTrue(self.pipeline._start_delivery(key, replacement, NOW + 31))

    def test_access_expiring_between_claim_and_send_keeps_unattempted(self):
        checks = 0
        def access(_):
            nonlocal checks
            checks += 1
            return checks == 1
        result = self.deliver(lambda *_: self.fail('Expired access'), access)
        self.assertEqual(result['denied'], 1)
        self.assertEqual(self.status(), 'pending')
        self.assertEqual(self.pipeline.db.execute('SELECT count(*) FROM delivery_claims').fetchone()[0], 0)

    def test_current_filter_threshold_stop_and_source_rechecked(self):
        for change in ({'filters': {'price_max': 100}}, {'min_discount': 99},
                       {'stopped': True}, {'enabled': False}, {'ready': False},
                       {'source': 'AUTO.RIA'}, {'paid': False}):
            with self.subTest(change=change):
                calls = 0
                def snapshot(*_):
                    nonlocal calls
                    calls += 1
                    return current() if calls == 1 else current(**change)
                result = self.deliver(lambda *_: self.fail('Changed search sent'),
                                      current_search=snapshot)
                self.assertEqual(result['denied'], 1)
                self.assertEqual(self.status(), 'pending')

    def test_policy_read_failure_defers_with_reason(self):
        def failure(_):
            raise RuntimeError('Private diagnostic must not be stored')
        result = self.deliver(lambda *_: self.fail('Technical failure sent'), failure)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(self.status(), 'pending')
        row = self.pipeline.db.execute('SELECT reason FROM delivery_diagnostics').fetchone()
        self.assertEqual(row[0], 'policy_or_evidence_read_failed')
        self.assertEqual(self.deliver()['accepted'], 1)

    def test_current_search_read_failure_after_claim_defers(self):
        calls = 0
        def snapshot(*_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError('Read temporarily unavailable')
            return current()
        result = self.deliver(lambda *_: self.fail('Read failure sent'), current_search=snapshot)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(self.status(), 'pending')

    def test_timeout_and_unknown_ack_are_not_replayed(self):
        def timeout(*_):
            raise TimeoutError()
        self.assertEqual(self.deliver(timeout)['uncertain'], 1)
        self.assertEqual(self.deliver(lambda *_: self.fail('Timeout replay'))['accepted'], 0)
        self.pipeline.db.execute("UPDATE deliveries SET status='pending'")
        self.pipeline.db.commit()
        self.assertEqual(self.deliver(lambda *_: None)['uncertain'], 1)
        self.assertEqual(self.deliver(lambda *_: self.fail('Unknown ack replay'))['accepted'], 0)

    def test_renderer_clock_advance_rechecks_proof_expiry(self):
        clock = [NOW]
        def renderer(car, assessment):
            clock[0] = NOW + 301
            return {'text': 'Synthetic'}
        result = self.deliver(lambda *_: self.fail('Expired proof sent'),
                              now=lambda: clock[0], renderer=renderer)
        self.assertEqual(result['held'], 1)
        self.assertEqual(self.status(), 'needs_revalidation')

    def test_switch_disabled_during_render_stops_attempt(self):
        switch = [True]
        def renderer(car, assessment):
            switch[0] = False
            return {'text': 'Synthetic'}
        result = self.deliver(lambda *_: self.fail('Disabled OLX sent'),
                              olx_enabled=lambda: switch[0], renderer=renderer)
        self.assertEqual(result['denied'], 1)
        self.assertEqual(self.status(), 'pending')

    def test_all_new_mode_allows_unknown_market_without_claiming_deal(self):
        self.pipeline.collect(lambda _: {'items': [raw('unknown', generation=None)], 'next': None},
                              NOW, page_budget=1, row_budget=10)
        all_new = users()[0]
        all_new['only_deals'] = False
        self.assertEqual(self.pipeline.enqueue([all_new], [], NOW,
            capacity=10, olx_enabled=True)['queued'], 1)
        delivered = []
        result = self.deliver(lambda uid, payload: delivered.append(uid) or True,
                             current_search=lambda *_: current(only_deals=False))
        self.assertEqual(result['accepted'], 2)
        saved = self.pipeline.db.execute("SELECT result FROM assessments WHERE id='unknown'").fetchone()[0]
        self.assertEqual(json.loads(saved)['status'], 'profitability_unconfirmed')

    def test_switch_from_all_new_to_deals_holds_unknown_market(self):
        self.pipeline.db.execute('DELETE FROM deliveries')
        self.pipeline.db.commit()
        self.pipeline.collect(lambda _: {'items': [raw('unknown', generation=None)], 'next': None},
                              NOW, page_budget=1, row_budget=10)
        all_new = users()[0]
        all_new['only_deals'] = False
        self.pipeline.enqueue([all_new], [], NOW, capacity=10, olx_enabled=True)
        result = self.deliver(lambda *_: self.fail('Unknown deal sent'))
        self.assertEqual(result['denied'], 2)

    def test_region_suffix_matches_without_mutating_stored_facts(self):
        car = copy.deepcopy(self.pipeline.cars()[0])
        car['region'] = 'Київська область'
        self.assertTrue(filtered(car, {'region': ['КИЇВСЬКА']}))
        self.assertFalse(filtered(car, {'region': ['Львівська']}))
        self.assertEqual(car['region'], 'Київська область')

    def test_paid_first_or_unpaid_first_does_not_block_later_paid(self):
        self.pipeline.db.execute('DELETE FROM deliveries')
        self.pipeline.db.commit()
        audience = users(3)
        audience[0]['paid'] = False
        result = self.pipeline.enqueue(audience, comps(), NOW, capacity=10, olx_enabled=True)
        self.assertEqual(result['queued'], 2)
        self.assertEqual(result['denied'], 1)
        sent = []
        delivered = self.deliver(lambda uid, payload: sent.append(uid) or True)
        self.assertEqual(delivered['accepted'], 2)
        self.assertEqual(set(sent), {'1', '2'})

    def test_active_attempt_counts_towards_capacity(self):
        self.assertIsNotNone(self.pipeline._claim_delivery(('0', 'olx', 'car'), NOW))
        self.pipeline.collect(lambda _: {'items': [raw('other')], 'next': None},
                              NOW, page_budget=1, row_budget=10)
        result = self.pipeline.enqueue(users(), comps(), NOW, capacity=1, olx_enabled=True)
        self.assertEqual(result['queued'], 0)
        self.assertEqual(result['overflow'], 1)


if __name__ == '__main__':
    unittest.main()
