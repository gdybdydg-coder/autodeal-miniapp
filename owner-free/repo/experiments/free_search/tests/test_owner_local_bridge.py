import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session
import test_owner_queue_sender as fixtures
from backend.models import Delivery,DeliveryTiming,Listing,User,MonitorMatch,MonitorSeen
from experiments.free_search.owner_pair_observer import observe_pairs
from experiments.free_search.owner_comparison_delivery import metadata
from experiments.free_search.owner_local_bridge import OwnerBridge,child_environment,atomic_json,LeasePublisher
from experiments.free_search.owner_automated_review import automated_review
from experiments.free_search.run_owner_service import preflight,acquire_service
from experiments.free_search.owner_night_runtime import Coordinator

class LocalBridgeTests(TestCase):
    def setUp(self):
        fixtures.QueueSenderTests.setUp(self);metadata.create_all(self.engine)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'continuous-browser-outbox').mkdir()
        self.capture={'kind':'public_visible_body','listing_id':self.page.listing_id,
          'listing_url':self.page.url,'observed_at':self.now,
          'text':'ID авто '+self.page.listing_id+'\n8 000 $\nСередня ціна\nОпис автомобіля\n'+('Відкритий опис автомобіля. '*5)+'\nІсторія авто\nКорозія порогів'}
        self.result={'listing_id':self.page.listing_id,'state':'captured','search_ids':[111],
                    'visible_review':self.capture}
        self.path=self.root/'continuous-browser-outbox'/(self.page.listing_id+'.result.json')
        self.path.write_text(json.dumps(self.result),encoding='utf-8')
        def transport(uid,card):
            self.calls.append((uid,card));return {'ok':True,'result':{'message_id':987}}
        self.bridge=OwnerBridge(self.engine,self.settings,self.pipeline,transport,self.root,clock=lambda:self.now)

    def test_live_gate_to_fake_transport_and_no_paid_identity(self):
        self.assertEqual(self.bridge.refresh()['status'],'readonly_ok')
        result=self.bridge.deliver_ready()
        self.assertEqual(result[0]['state'],'sent',result)
        self.assertEqual(self.bridge.deliver_ready(),[])
        self.assertEqual(len(self.calls),1)
        self.assertIn('🧪 Тест',self.calls[0][1]['text'])
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(Delivery.id)))
            self.assertIsNone(db.scalar(select(Listing.id)))

    def test_revocation_blocks_and_snapshot_cannot_authorize(self):
        self.bridge.refresh()
        with Session(self.engine) as db,db.begin():db.get(User,111).ready=False
        self.assertEqual(self.bridge.deliver_ready()[0]['state'],'blocked')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.bridge.refresh()['status'],'blocked')

    def test_missing_history_fails_closed(self):
        self.result['visible_review']['text']='ID авто '+self.page.listing_id+'\nОпис '+('Опис. '*100)
        self.path.write_text(json.dumps(self.result),encoding='utf-8')
        self.assertEqual(self.bridge.deliver_ready()[0]['reason'],'visible_history_section_unavailable')
        self.assertEqual(self.calls,[])

    def test_uncertain_is_terminal(self):
        self.bridge.transport=lambda *a:None
        self.assertEqual(self.bridge.deliver_ready()[0]['state'],'uncertain')
        self.assertEqual(self.bridge.deliver_ready(),[])

    def test_review_preserves_risk_and_never_claims_full_history(self):
        review=automated_review(self.capture,self.page,self.now)
        self.assertIn('Корозія',review['findings'][0])
        self.assertEqual(review['review_method'],'automated_visible_text_only')
        self.assertIn('не підтверджені',review['findings'][-1])
        self.capture['observed_at']-=301
        with self.assertRaisesRegex(ValueError,'stale'):automated_review(self.capture,self.page,self.now)

    def test_environment_has_no_secret_passthrough(self):
        result=child_environment({'PATH':'path','SystemRoot':'windows','DATABASE_URL':'secret',
            'AUTODEAL_OWNER_TELEGRAM_TOKEN':'secret','UNFORESEEN_SECRET':'secret','HTTP_PROXY':'secret'})
        self.assertEqual(result,{'PATH':'path','SystemRoot':'windows'})

    def test_windows_reader_collision_retries_without_partial_lease(self):
        import os
        target=self.root/'lease.json';atomic_json(target,{'enabled':False})
        replace=os.replace;attempts=[]
        def collision(source,destination):
            attempts.append(1)
            if len(attempts)<3:
                self.assertEqual(json.loads(target.read_text()),{'enabled':False})
                raise PermissionError('reader sharing')
            return replace(source,destination)
        with patch('experiments.free_search.owner_local_bridge.os.replace',side_effect=collision),patch('experiments.free_search.owner_local_bridge.time.sleep'):
            atomic_json(target,{'enabled':True})
        self.assertEqual(json.loads(target.read_text()),{'enabled':True})
        self.assertEqual(len(attempts),3);self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_persistent_write_denial_preserves_previous_lease_and_stops(self):
        target=self.root/'lease.json';atomic_json(target,{'enabled':False})
        with patch('experiments.free_search.owner_local_bridge.os.replace',side_effect=PermissionError('denied')) as replace,patch('experiments.free_search.owner_local_bridge.time.sleep'):
            with self.assertRaises(PermissionError):atomic_json(target,{'enabled':True})
        self.assertEqual(replace.call_count,10)
        self.assertEqual(json.loads(target.read_text()),{'enabled':False})
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_lease_continues_without_fabricating_new_access_time(self):
        import threading,time
        seen=threading.Event();values=[];real=atomic_json
        def record(path,value):
            real(path,value);values.append(value)
            if len(values)>=3:seen.set()
        publisher=LeasePublisher(self.root/'lease.json',lambda:{'heartbeat':time.time(),'access_at':100},interval=.01)
        with patch('experiments.free_search.owner_local_bridge.atomic_json',side_effect=record):
            publisher.start()
            try:self.assertTrue(seen.wait(timeout=2))
            finally:publisher.stop()
        self.assertTrue(all(v['access_at']==100 for v in values))
        self.assertGreater(values[-1]['heartbeat'],values[0]['heartbeat'])
        self.assertFalse(publisher.thread.is_alive())

    def test_lease_write_failure_is_visible_and_stops(self):
        import time
        publisher=LeasePublisher(self.root/'lease.json',lambda:{'access_at':100},interval=.01)
        with patch('experiments.free_search.owner_local_bridge.atomic_json',side_effect=[None,PermissionError('sharing')]):
            publisher.start();self.assertTrue(publisher.halt.wait(timeout=2));publisher.stop()
        self.assertEqual(publisher.error,'PermissionError')

    def test_price_change_between_intake_and_browser_cannot_send(self):
        self.result['visible_review']['text']=self.capture['text'].replace('8 000 $','18 000 $')
        self.path.write_text(json.dumps(self.result),encoding='utf-8')
        self.assertEqual(self.bridge.deliver_ready()[0]['reason'],'visible_asking_price_changed')
        self.assertEqual(self.calls,[])

    def test_preflight_never_reports_secret_values(self):
        result=preflight({'AUTODEAL_OWNER_DATABASE_URL':'password','AUTODEAL_OWNER_ID':'1',
                         'SUBSCRIPTION_EXPECTED_ADMIN_ID':'2'},node='node',browser_dependency=True)
        self.assertFalse(result['ready']);self.assertNotIn('password',json.dumps(result))

    def test_shared_lock_no_takeover(self):
        guard=Coordinator(self.root/'lock.sqlite3')
        try:
            acquire_service(guard,'original',100)
            with self.assertRaisesRegex(ValueError,'busy'):acquire_service(guard,'other',10000000)
            self.assertTrue(guard.owns('original'))
            guard.release('original')
            acquire_service(guard,'next',10000001)
            self.assertTrue(guard.owns('next'))
        finally:guard.close()

    def seed_paid_pair(self,*,owner=111,epoch='epoch111',message_id=456):
        self.assertEqual(self.bridge.deliver_ready()[0]['state'],'sent')
        with Session(self.engine) as db,db.begin():
            db.add(Listing(id=880,source='auto_ria',source_id=self.page.listing_id,car={}))
            db.add(Delivery(id=881,user_id=owner,listing_id=880,state='sent',message_id=message_id))
            db.add(DeliveryTiming(delivery_id=881,queued_at=self.now-3,accepted_at=self.now-2))
            db.add(MonitorMatch(search_id=owner,listing_id=880,epoch=epoch,fingerprint='fixture'))
            db.add(MonitorSeen(search_id=owner,source_id=self.page.listing_id,epoch=epoch,state='seen',first_seen=self.now-5))

    def test_same_identity_pair_reports_actual_receipt_delta(self):
        self.seed_paid_pair()
        report=observe_pairs(self.engine,111,clock=lambda:self.now)
        self.assertEqual(report['matched_pairs'],1)
        pair=report['pairs'][0]
        self.assertEqual(pair['paid_message_id'],456)
        self.assertEqual(pair['free_minus_paid_accepted_seconds'],2)
        self.assertIsNone(pair['publication_at'])
        self.assertFalse(report['phone_push_time_known'])

    def test_other_owner_receipt_cannot_form_pair(self):
        self.seed_paid_pair(owner=222)
        report=observe_pairs(self.engine,111,clock=lambda:self.now)
        self.assertEqual(report['matched_pairs'],0)
        self.assertIsNone(report['pairs'][0]['free_minus_paid_accepted_seconds'])
        self.assertEqual(observe_pairs(self.engine,222,clock=lambda:self.now)['pairs'],[])

    def test_other_epoch_receipt_and_old_diagnostic_are_not_pairs(self):
        self.seed_paid_pair(epoch='old',message_id=27366)
        report=observe_pairs(self.engine,111,clock=lambda:self.now)
        self.assertEqual(report['matched_pairs'],0)
        self.assertIsNone(report['pairs'][0]['free_minus_paid_first_seen_seconds'])
