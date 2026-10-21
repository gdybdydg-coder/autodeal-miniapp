"""Disposable SQLite + fabricated HTML only. No production fixtures or keys."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import json
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from backend.models import Base, Delivery, Filters, Listing, MonitorWatch, Search, User
from backend.billing_models import Entitlement
from backend.manual_payment_models import ManualBase, PaymentRequest
from backend import paid_source_access
from experiments.free_search.owner_access import current_searches, revalidate
from experiments.free_search.owner_pilot import PublicPage, prepare_asking_research as prepare

NOW = 1791800000
OWNER, CLIENT = 111, 222  # Synthetic, never taken from a production account.


def page(ident='12345678', price=8000, **changes):
    url = 'https://auto.ria.com/uk/auto_example_model_' + ident + '.html'
    node = {'@type': 'Vehicle', '@id': url, 'url': url,
            'brand': {'name': 'Example'}, 'model': 'Model', 'productionDate': '2016',
            'fuelType': 'Бензин', 'vehicleTransmission': 'Автомат',
            'mileageFromOdometer': {'value': 100000, 'unitCode': 'KMT'},
            'offers': {'@type': 'Offer', 'price': price, 'priceCurrency': 'USD',
                       'availability': 'https://schema.org/InStock'}}
    node.update(changes)
    html = ('<a>Легкові з пробігом</a><a>Вінницька область</a><h1>Example</h1>'
            '<div>ID авто ' + ident + '</div>'
            '<script type="application/ld+json">' + json.dumps(node) + '</script>'
            '<div id="basicInfoPriceGhost">Середня ціна: 10 500 $</div>'
            '<div id="basicInfoPrice">' + str(price) + ' $</div>'
            '<div id="descCharacteristicsValue">Седан</div>'
            '<div id="descEngineEngine">Бензин, 2.0 л</div>'
            '<div id="basicInfoGenerationBase">II</div>'
            '<div id="descDriveTypeDriveType">Передній</div>')
    return PublicPage(ident, url, html, NOW - 10)


class OwnerPilotTests(TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'SUBSCRIPTION_EXPECTED_ADMIN_ID': str(OWNER)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.engine = create_engine('sqlite:///:memory:')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine); ManualBase.metadata.create_all(self.engine)
        self.engine.update_execution_options(**{paid_source_access.OPTION: True,
                                               paid_source_access.EXCLUSIONS: ()})
        self.settings = SimpleNamespace(live=True, admin_telegram_id=OWNER)
        f = Filters(brand='Example', region=['Вінницька область', 'Чернівецька область'])
        with Session(self.engine) as db, db.begin():
            for uid in (OWNER, CLIENT):
                db.add(User(id=uid, ready=True))
                db.add(PaymentRequest(id=str(uid), user_id=uid, state='approved',
                    amount_minor=25000, currency='UAH', days=30,
                    created_at=NOW-100, updated_at=NOW-100, expires_at=NOW+1000))
                db.add(Entitlement(user_id=uid, expires_at=NOW+1000, updated_at=NOW-100))
                db.add(Search(id=uid, user_id=uid, name='Fixture', enabled=True,
                    fingerprint=f.fingerprint(), filters=f.canonical()))
                db.add(MonitorWatch(search_id=uid, epoch='epoch'+str(uid)))
        self.snapshot = current_searches(self.engine, self.settings, NOW)[0][0]
        self.subject = page()
        self.peers = [page(str(20000000+i), 10000+i*100) for i in range(6)]

    def result(self, **changes):
        args = dict(engine=self.engine, settings=self.settings, snapshot=self.snapshot,
                    subject=self.subject, peers=self.peers,
                    frozen_peer_ids=[p.listing_id for p in self.peers], now=NOW)
        args.update(changes)
        return prepare(**args)

    def test_paid_client_never_selected_as_owner(self):
        searches, reason = current_searches(self.engine, self.settings, NOW)
        self.assertEqual(reason, 'eligible'); self.assertEqual([s.search_id for s in searches], [OWNER])
        self.assertEqual(self.result(snapshot=replace(self.snapshot, search_id=CLIENT))['reason'], 'owner_search_changed')

    def test_local_preview_is_never_live_or_proven_new(self):
        r = self.result()
        self.assertEqual(r['status'], 'local_draft')
        self.assertFalse(r['ready_for_delivery']); self.assertIsNone(r['publication_at'])
        self.assertIn('Орієнтир за цінами аналогів', r['draft'])
        self.assertNotIn('ринкова ціна', r['draft']); self.assertEqual(r['paid_calls'], 0)

    def test_owner_role_does_not_bypass_payment(self):
        with Session(self.engine) as db, db.begin():
            db.get(PaymentRequest, str(OWNER)).state = 'review'
        self.assertEqual(self.result()['reason'], 'owner_confirmed_payment_required')

    def test_expired_payment_denied(self):
        self.assertEqual(self.result(now=NOW+1001)['reason'], 'owner_confirmed_payment_required')

    def test_stop_after_snapshot_denied(self):
        with Session(self.engine) as db, db.begin(): db.get(User, OWNER).ready = False
        self.assertEqual(self.result()['reason'], 'owner_stopped')

    def test_search_disabled_after_snapshot_denied(self):
        with Session(self.engine) as db, db.begin(): db.get(Search, OWNER).enabled = False
        self.assertEqual(self.result()['reason'], 'no_enabled_owner_search')

    def test_search_edit_and_new_epoch_invalidates_snapshot(self):
        with Session(self.engine) as db, db.begin(): db.get(MonitorWatch, OWNER).epoch = 'new'
        self.assertEqual(self.result()['reason'], 'owner_search_changed')

    def test_unknown_owner_identity_denied(self):
        self.settings.admin_telegram_id = CLIENT
        self.assertEqual(self.result()['reason'], 'owner_not_verified')

    def test_non_strict_policy_denied(self):
        self.engine.update_execution_options(**{paid_source_access.OPTION: False})
        self.assertEqual(self.result()['reason'], 'strict_paid_policy_required')

    def test_existing_ria_delivery_never_replayed(self):
        with Session(self.engine) as db, db.begin():
            db.add(Listing(id=1, source='auto_ria', source_id=self.subject.listing_id, car={}))
            db.add(Delivery(id=1, user_id=OWNER, listing_id=1, state='uncertain'))
        for state in ('uncertain', 'sent', 'pending', 'failed', 'cancelled'):
            with Session(self.engine) as db, db.begin(): db.get(Delivery, 1).state = state
            self.assertEqual(self.result()['reason'], 'existing_owner_delivery')

    def test_other_client_delivery_does_not_authorize_or_block_owner(self):
        with Session(self.engine) as db, db.begin():
            db.add(Listing(id=1, source='auto_ria', source_id=self.subject.listing_id, car={}))
            db.add(Delivery(user_id=CLIENT, listing_id=1, state='sent'))
        self.assertEqual(self.result()['status'], 'local_draft')

    def test_missing_and_bad_peers_stay_in_denominator(self):
        r = self.result(peers=self.peers[:3])
        self.assertEqual(r['status'], 'unknown'); self.assertEqual(r['planned_peers'], 6)
        self.assertEqual(len(r['peer_errors']), 3)

    def test_drive_mismatch_is_not_comparable(self):
        pp = [replace(p, html=p.html.replace('Передній', 'Повний')) for p in self.peers]
        r = self.result(peers=pp)
        self.assertEqual(r['status'], 'unknown'); self.assertEqual(r['prepared_peers'], 0)

    def test_missing_target_generation_not_invented(self):
        target = replace(self.subject, html=self.subject.html.replace(
            '<div id="basicInfoGenerationBase">II</div>', ''))
        self.assertEqual(self.result(subject=target)['status'], 'unknown')

    def test_old_price_rejected(self):
        r = self.result(subject=replace(self.subject, received_at=NOW-301))
        self.assertEqual(r['reason'], 'public_page_identity_or_freshness_invalid')

    def test_foreign_url_rejected(self):
        r = self.result(subject=replace(self.subject, url=self.subject.url.replace('auto.ria.com', 'example.invalid')))
        self.assertEqual(r['reason'], 'public_page_identity_or_freshness_invalid')

    def test_holdout_leakage_and_duplicate_rejected(self):
        for ids in ([self.subject.listing_id], [self.peers[0].listing_id]*2):
            with self.assertRaises(ValueError): self.result(frozen_peer_ids=ids)

    def test_price_mismatch_stops(self):
        target = replace(self.subject, html=self.subject.html.replace('8000 $', '100 $'))
        self.assertEqual(self.result(subject=target)['reason'], 'visible_price_missing_or_conflicting')

    def test_current_filters_really_applied(self):
        f = Filters(brand='Other')
        with Session(self.engine) as db, db.begin():
            s = db.get(Search, OWNER); s.filters = f.canonical(); s.fingerprint = f.fingerprint()
        self.assertEqual(self.result()['reason'], 'owner_search_changed')
        fresh = current_searches(self.engine, self.settings, NOW)[0][0]
        self.assertEqual(self.result(snapshot=fresh)['reason'], 'owner_filter_mismatch')

    def test_threshold_uses_current_saved_percent(self):
        f = Filters(brand='Example', minDiscount=50.0)
        with Session(self.engine) as db, db.begin():
            s = db.get(Search, OWNER); s.filters = f.canonical(); s.fingerprint = f.fingerprint()
        fresh = current_searches(self.engine, self.settings, NOW)[0][0]
        self.assertEqual(self.result(snapshot=fresh)['status'], 'below_threshold')

    def test_prepare_has_no_db_mutations(self):
        statements = []
        def observe(conn, cursor, statement, parameters, context, many): statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', observe)
        self.result()
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))

    def test_stop_during_valuation_blocks_draft(self):
        from experiments.free_search.valuation import estimate
        def stopped(*args):
            result = estimate(*args)
            with Session(self.engine) as db, db.begin(): db.get(User, OWNER).ready = False
            return result
        with patch('experiments.free_search.owner_pilot.estimate', stopped):
            result = self.result()
        self.assertEqual(result['reason'], 'owner_stopped'); self.assertIsNone(result['draft'])

    def test_missing_optional_fuel_and_damage_do_not_ban_whole_car(self):
        subject = page(fuelType=None, vehicleTransmission=None,
                       itemCondition='https://schema.org/DamagedCondition')
        self.assertEqual(self.result(subject=subject)['status'], 'local_draft')

    def test_unplanned_peer_cannot_enter_comparison(self):
        with self.assertRaisesRegex(ValueError, 'unplanned_or_duplicate_peer'):
            self.result(peers=self.peers + [page('99999999', 10000)])
