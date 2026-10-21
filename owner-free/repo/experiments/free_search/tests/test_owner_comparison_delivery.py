from unittest import TestCase
from sqlalchemy import select
from sqlalchemy.orm import Session
import test_owner_queue_sender as fixtures
from backend.models import Delivery,DeliveryTiming,Listing,User,Search
from experiments.free_search.owner_comparison_delivery import metadata,deliveries,send_comparison


class ComparisonDeliveryTests(TestCase):
    def setUp(self):
        fixtures.QueueSenderTests.setUp(self)
        metadata.create_all(self.engine)

    def send_free(self,transport=None):
        def accepted(uid,text):
            self.calls.append((uid,text))
            return {'ok':True,'result':{'message_id':555}}
        return send_comparison(self.engine,self.settings,self.pipeline,self.page.listing_id,
            111,transport or accepted,condition_review=self.review,enabled=True,clock=lambda:self.now)

    def test_free_receipt_does_not_claim_paid_delivery_or_listing(self):
        result=self.send_free()
        self.assertEqual(result['state'],'sent',result)
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(Delivery.id)))
            self.assertIsNone(db.scalar(select(Listing.id)))
        self.assertEqual(self.send_free()['reason'],'existing_free_comparison')
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.calls[0][1]['text'].startswith('🧪 Тест\n'))
        self.assertNotIn('UTC',self.calls[0][1]['text'])

    def test_existing_paid_delivery_remains_and_free_can_also_send(self):
        with Session(self.engine) as db,db.begin():
            listing=Listing(source='auto_ria',source_id=self.page.listing_id,car={'pipeline':{'kind':'paid_fixture'}})
            db.add(listing);db.flush()
            db.add(Delivery(user_id=111,listing_id=listing.id,state='sent',message_id=444))
        result=self.send_free()
        self.assertEqual(result['state'],'sent',result)
        with Session(self.engine) as db:
            paid=db.scalars(select(Delivery)).one()
            self.assertEqual((paid.state,paid.message_id),('sent',444))

    def test_ambiguous_transport_never_retried(self):
        self.assertEqual(self.send_free(lambda *args:None)['state'],'uncertain')
        self.assertEqual(self.send_free()['reason'],'existing_free_comparison')
        self.assertEqual(self.calls,[])

    def test_crash_after_transport_keeps_durable_claim(self):
        def crash(*args):raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):self.send_free(crash)
        self.assertEqual(self.send_free()['reason'],'existing_free_comparison')
        with self.engine.connect() as db:
            self.assertEqual(db.scalar(select(deliveries.c.state)),'claimed')

    def test_owner_stop_and_stale_data_block_before_claim(self):
        with Session(self.engine) as db,db.begin():db.get(User,111).ready=False
        self.assertEqual(self.send_free()['reason'],'owner_stopped')
        with self.engine.connect() as db:self.assertIsNone(db.scalar(select(deliveries.c.state)))

    def test_saved_filters_and_percentage_are_rechecked(self):
        with Session(self.engine) as db,db.begin():
            search=db.get(Search,111)
            from backend.models import Filters
            search.filters={**search.filters,'minDiscount':99}
            search.fingerprint=Filters(**search.filters).fingerprint()
        self.assertEqual(self.send_free()['state'],'blocked')
        self.assertEqual(self.calls,[])

    def test_recent_paid_send_waits_without_claim(self):
        with Session(self.engine) as db,db.begin():
            listing=Listing(source='auto_ria',source_id='12345678',car={})
            db.add(listing);db.flush()
            delivery=Delivery(user_id=111,listing_id=listing.id,state='sent',message_id=444)
            db.add(delivery);db.flush()
            db.add(DeliveryTiming(delivery_id=delivery.id,queued_at=self.now,send_started_at=self.now))
        self.assertEqual(self.send_free()['reason'],'chat_interval')
        with self.engine.connect() as db:self.assertIsNone(db.scalar(select(deliveries.c.state)))
