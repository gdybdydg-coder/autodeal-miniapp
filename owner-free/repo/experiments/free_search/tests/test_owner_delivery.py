import tempfile
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.models import Delivery, DeliveryTiming, User
import test_owner_pilot as fixtures
from experiments.free_search.owner_delivery import send_one
from experiments.free_search.owner_observations import Observations
from experiments.free_search.public_cards import PublicCard


class OwnerDeliveryTests(fixtures.TestCase):
    def setUp(self):
        fixtures.OwnerPilotTests.setUp(self)
        from unittest.mock import patch
        layout = patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED', True)
        layout.start(); self.addCleanup(layout.stop)

    def send(self, transport=None, **options):
        if not hasattr(self, 'obs'):
            temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
            self.obs = Observations(Path(temp.name)/'test.owner-observations.sqlite3')
            self.addCleanup(self.obs.close)
            def card(ident):
                return PublicCard(ident, fixtures.page(ident).url, None, None, None, False, (), ())
            self.obs.ingest([card('98765432')], fixtures.NOW-30)
            self.obs.ingest([card(self.subject.listing_id)], fixtures.NOW-10)
        return send_one(self.engine, self.settings, self.snapshot, self.subject,
            self.peers, [p.listing_id for p in self.peers], self.obs,
            transport or (lambda uid, text: {'ok': True, 'result': {'message_id': 10}}),
            enabled=options.get('enabled', True), clock=lambda: fixtures.NOW,
            browser_mean=__import__('test_owner_browser_mean').capture(self.subject))

    def test_disabled_has_no_claim(self):
        self.assertEqual(self.send(enabled=False)['state'], 'disabled')
        with Session(self.engine) as db: self.assertIsNone(db.scalar(select(Delivery.id)))

    def test_first_seen_to_owner_receipt_and_no_replay(self):
        calls = []
        def transport(uid, text):
            calls.append((uid, text))
            return {'ok': True, 'result': {'message_id': 123, 'date': fixtures.NOW}}
        self.assertEqual(self.send(transport)['state'], 'sent')
        self.assertEqual(self.send(transport)['reason'], 'existing_owner_delivery')
        self.assertEqual(len(calls), 1); self.assertEqual(calls[0][0], fixtures.OWNER)
        self.assertIn('Нове для нашого спостереження', calls[0][1])
        with Session(self.engine) as db:
            timing = db.scalar(select(DeliveryTiming))
            self.assertIsNone(timing.source_added_at)
            self.assertEqual(timing.accepted_at-timing.discovered_at, 10)

    def test_ambiguous_transport_never_retried(self):
        calls = []
        def transport(*args):
            calls.append(1); raise TimeoutError()
        self.assertEqual(self.send(transport)['state'], 'free_uncertain')
        self.assertEqual(self.send(transport)['reason'], 'existing_owner_delivery')
        self.assertEqual(len(calls), 1)

    def test_stop_between_preparation_and_claim(self):
        from experiments.free_search.owner_delivery import prepare
        from unittest.mock import patch
        def stopped(*args, **kwargs):
            result = prepare(*args, **kwargs)
            with Session(self.engine) as db, db.begin(): db.get(User, fixtures.OWNER).ready = False
            return result
        with patch('experiments.free_search.owner_delivery.prepare', stopped):
            self.assertEqual(self.send()['reason'], 'owner_stopped')
        with Session(self.engine) as db: self.assertIsNone(db.scalar(select(Delivery.id)))

    def test_baseline_and_old_first_seen_not_resent(self):
        self.send(enabled=False)
        self.assertEqual(self.obs.signal('98765432', fixtures.NOW)['reason'], 'baseline_listing')
        self.assertEqual(self.obs.signal(self.subject.listing_id, fixtures.NOW+301)['reason'], 'stale_observation')

    def test_zero_peers_can_send_with_synthetic_provider_mean(self):
        self.peers = []
        self.assertEqual(self.send()['state'], 'sent')

    def test_unverified_real_layout_never_claims_or_transports(self):
        from unittest.mock import patch
        with patch('test_owner_browser_mean.capture', return_value=None):
            result = self.send(lambda *args: self.fail('unverified mean transported'))
        self.assertEqual(result['reason'], 'public_lower_bound_unverified')
        with Session(self.engine) as db: self.assertIsNone(db.scalar(select(Delivery.id)))

    def test_real_adapter_refuses_other_recipient_before_network(self):
        from experiments.free_search.owner_telegram import TelegramText
        adapter = TelegramText('123:'+('x'*30), fixtures.OWNER)
        with self.assertRaisesRegex(ValueError, 'invalid_owner_message'):
            adapter(fixtures.CLIENT, 'test')

    def test_crash_after_claim_leaves_durable_no_replay(self):
        def crash(*args): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.send(crash)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(Delivery.state)), 'free_claimed')
        self.assertEqual(self.send()['reason'], 'existing_owner_delivery')

    def test_stop_after_committed_claim_prevents_transport(self):
        from experiments.free_search.owner_delivery import _access
        from unittest.mock import patch
        calls = []
        def checked(db, settings, snapshot, clock):
            calls.append(1)
            if len(calls) == 2:
                db.get(User, fixtures.OWNER).ready = False
                db.flush()
            return _access(db, settings, snapshot, clock)
        with patch('experiments.free_search.owner_delivery._access', checked):
            result = self.send(lambda *args: self.fail('transport after stop'))
        self.assertEqual(result['state'], 'free_cancelled')
        self.assertEqual(result['reason'], 'owner_stopped')
