"""Execute the exact proposed diff in memory against synthetic SQLite only."""
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.billing_models import Entitlement
from backend.manual_payment_models import PaymentRequest
from backend.models import Delivery, User
from backend.monitor import access_allowed_clause
import test_owner_pilot as fixtures
import test_owner_delivery as delivery_fixture
from experiments.free_search.owner_access_proposal import load_review_modules, proposal


class AccessProposalTests(fixtures.TestCase):
    def setUp(self):
        fixtures.OwnerPilotTests.setUp(self)
        layout = patch('experiments.free_search.owner_public_mean.NUMERIC_LAYOUT_VERIFIED', True)
        layout.start(); self.addCleanup(layout.stop)
        self.policy, self.access, self.sender = load_review_modules()
        self.snapshot = self.access.current_searches(self.engine, self.settings, fixtures.NOW)[0][0]
        self.patch_preview = patch('experiments.free_search.owner_pilot.revalidate', self.access.revalidate)
        self.patch_preview.start()
        self.addCleanup(self.patch_preview.stop)

    def exclude_jobs(self):
        self.engine.update_execution_options(**{self.policy.PAID_JOB_EXCLUSIONS: (fixtures.OWNER,)})

    def send(self, transport=None):
        with patch.object(delivery_fixture, 'send_one', self.sender.send_one):
            return delivery_fixture.OwnerDeliveryTests.send(self, transport)

    def test_default_candidate_preserves_owner_and_client_paid_access(self):
        with Session(self.engine) as db:
            for uid in (fixtures.OWNER, fixtures.CLIENT):
                self.assertTrue(self.policy.allowed(db, uid, fixtures.NOW))
                self.assertTrue(self.policy.confirmed_allowed(db, uid, fixtures.NOW))
        self.assertEqual(self.policy.exclusions(self.engine), ())

    def test_job_exclusion_blocks_paid_queries_but_free_receipt_and_dedup_work(self):
        self.exclude_jobs()
        with Session(self.engine) as db:
            self.assertFalse(self.policy.allowed(db, fixtures.OWNER, fixtures.NOW))
            self.assertTrue(self.policy.allowed(db, fixtures.CLIENT, fixtures.NOW))
            # The actual bulk SQL predicate used by monitor/full_scan.
            ids = list(db.scalars(select(User.id).where(access_allowed_clause(
                User.id, fixtures.NOW, **self.policy.query_options(self.engine)))))
            self.assertEqual(ids, [fixtures.CLIENT])
            self.assertTrue(self.policy.any_paid(db, fixtures.NOW))
        calls = []
        def transport(uid, text):
            calls.append(uid)
            return {'ok': True, 'result': {'message_id': 777}}
        self.assertEqual(self.send(transport)['state'], 'sent')
        self.assertEqual(self.send(transport)['reason'], 'existing_owner_delivery')
        self.assertEqual(calls, [fixtures.OWNER])
        with Session(self.engine) as db:
            self.assertTrue(db.get(User, fixtures.CLIENT).ready)
            self.assertEqual(db.get(Entitlement, fixtures.CLIENT).expires_at, fixtures.NOW+1000)
            self.assertIsNone(db.scalar(select(Delivery.id).where(Delivery.user_id == fixtures.CLIENT)))

    def test_stopped_owner_still_denied_after_job_exclusion(self):
        self.exclude_jobs()
        with Session(self.engine) as db, db.begin():
            db.get(User, fixtures.OWNER).ready = False
        self.assertEqual(self.send()['reason'], 'owner_stopped')

    def test_purchase_expiry_between_preview_and_claim_still_blocks(self):
        self.exclude_jobs()
        prepare = self.sender.prepare
        def expire(*args, **kwargs):
            result = prepare(*args, **kwargs)
            with Session(self.engine) as db, db.begin():
                db.get(PaymentRequest, str(fixtures.OWNER)).expires_at = fixtures.NOW
            return result
        with patch.object(self.sender, 'prepare', expire):
            self.assertEqual(self.send(lambda *a: self.fail('transport after expiry'))['reason'],
                             'owner_confirmed_payment_required')
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(Delivery.id)))

    def test_stop_after_claim_cancels_without_transport(self):
        self.exclude_jobs()
        access = self.sender._access
        calls = []
        def stop(db, settings, snapshot, clock):
            calls.append(1)
            if len(calls) == 2:
                db.get(User, fixtures.OWNER).ready = False
                db.flush()
            return access(db, settings, snapshot, clock)
        with patch.object(self.sender, '_access', stop):
            result = self.send(lambda *a: self.fail('transport after stop'))
        self.assertEqual(result['state'], 'free_cancelled')
        self.assertEqual(result['reason'], 'owner_stopped')

    def test_account_exclusion_and_entitlement_expiry_not_bypassed(self):
        self.exclude_jobs()
        self.engine.update_execution_options(**{self.policy.EXCLUSIONS: (fixtures.OWNER,)})
        self.assertEqual(self.send()['reason'], 'owner_confirmed_payment_required')
        self.engine.update_execution_options(**{self.policy.EXCLUSIONS: ()})
        with Session(self.engine) as db, db.begin():
            db.get(Entitlement, fixtures.OWNER).expires_at = fixtures.NOW
        self.assertEqual(self.send()['reason'], 'owner_confirmed_payment_required')

    def test_non_strict_policy_cannot_authorize_free_access(self):
        self.engine.update_execution_options(**{self.policy.OPTION: False})
        with Session(self.engine) as db:
            self.assertFalse(self.policy.confirmed_allowed(db, fixtures.OWNER, fixtures.NOW))
        self.assertEqual(self.send()['reason'], 'owner_policy_required')

    def test_proposal_has_no_activation_or_filesystem_mutation(self):
        from experiments.free_search.owner_access_proposal import REPO
        originals, proposed, forward, reverse = proposal()
        self.assertIn('confirmed_allowed', forward)
        self.assertIn('-def confirmed_allowed', reverse)
        for path, content in originals.items():
            self.assertEqual((REPO/path).read_text(encoding='utf-8'), content)
        self.assertNotIn('PAID_JOB_EXCLUSIONS:', proposed['backend/paid_source_access.py'])
