import os
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from experiments.free_search.durable_pipeline import DurablePipeline
from experiments.free_search.filter_gate import SearchFilter,Span
from experiments.free_search.network_guard import NetworkDenied,OutboundGuard

NOW=1000


def card(sid,published=990):
    return dict(listing_id=str(sid),published_at=published,url=f'https://auto.ria.com/uk/auto_fixture_{sid}.html',preview_usd=999999)


def details(row,**overrides):
    return dict(listing_id=row['id'],active=True,category='passenger',price_usd='7000',currency='USD',price='7000',region='kyiv',brand='Ford',model='Focus',year=2015,mileage_km=100000,observed_at=NOW,**overrides)


def estimate(subject,now):
    return dict(status='estimated',reference_price_usd='10000',reason='peer_estimate',provenance='experimental_peer_asking_v1')


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'fixture.free-pipeline.sqlite3'
        self.p=DurablePipeline(self.path)
        self.add_user(1)
        self.p.start_scan('first',NOW,800)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def add_user(self,uid,**kw):
        args=dict(started_at=900,access_until=2000)
        args.update(kw)
        self.p.put_recipient(uid,uid,SearchFilter(min_discount_percent=Decimal(10)),**args)

    def prepare(self,cards=None,loader=details):
        self.p.ingest_page('first',1,cards or [card(500)],has_more=False,now=NOW)
        self.p.process_details(loader,NOW)
        self.p.process_valuations(estimate,NOW)

    def accepted(self,claim,detail):
        return dict(outcome='accepted',message_id=claim['user_id'])

    def test_paginated_burst_over_two_pages_shared_details_all_cars(self):
        self.add_user(2)
        for page in range(1,6):
            self.p.ingest_page('first',page,[card(i) for i in range(page*30,page*30+30)],has_more=page<5,now=NOW)
        self.p.process_details(details,NOW)
        self.p.process_valuations(estimate,NOW)
        self.p.send_due(self.accepted,NOW)
        summary=self.p.summary(NOW)
        self.assertEqual((summary['unique_listings'],summary['detail_calls'],summary['claims']['sent']),(150,150,300))
        self.assertFalse(summary['source_coverage_proven'])

    def test_late_appearance_lower_id_and_changed_page_order(self):
        self.prepare([card(999)])
        self.p.start_scan('next',NOW+10,800)
        self.p.ingest_page('next',1,[card(999),card(100,980)],has_more=False,now=NOW+10)
        self.assertEqual({r['id'] for r in self.p.rows('listings')},{'999','100'})

    def test_preview_price_never_rejects_current_detail_price(self):
        self.p.put_recipient(1,1,SearchFilter(price_usd=Span(maximum=Decimal(8000))),started_at=900,access_until=2000)
        self.prepare()
        self.assertEqual(len(self.p.rows('claims')),1)

    def test_overflow_no_seen_or_cursor_advance_retry_after_drain(self):
        self.p.max_pending=2
        self.p.ingest_page('first',1,[card(1),card(2)],has_more=True,now=NOW)
        result=self.p.ingest_page('first',2,[card(3),card(4)],has_more=False,now=NOW)
        self.assertFalse(result['accepted'])
        self.assertEqual(self.p.rows('scans')[0]['next_page'],2)
        self.assertEqual(len(self.p.rows('listings')),2)
        self.p.process_details(details,NOW); self.p.process_valuations(estimate,NOW)
        self.assertTrue(self.p.ingest_page('first',2,[card(3),card(4)],has_more=False,now=NOW)['accepted'])
        self.assertEqual(len(self.p.rows('listings')),4)

    def test_restart_keeps_scan_cursor_and_durable_work(self):
        self.p.ingest_page('first',1,[card(1)],has_more=True,now=NOW)
        self.p.close(); self.p=DurablePipeline(self.path)
        self.p.recover(NOW+1)
        self.assertEqual(self.p.rows('scans')[0]['next_page'],2)
        self.p.ingest_page('first',2,[card(2)],has_more=False,now=NOW+1)
        self.assertEqual(len(self.p.rows('listings')),2)

    def test_page_limit_never_claims_full_coverage(self):
        self.p.start_scan('limited',NOW,800,page_budget=1)
        self.p.ingest_page('limited',1,[card(1)],has_more=True,now=NOW)
        row=[s for s in self.p.rows('scans') if s['id']=='limited'][0]
        self.assertEqual((row['state'],row['reason']),('incomplete','page_budget_reached'))

    def test_repeated_page_and_changed_retry_are_explicit(self):
        self.p.ingest_page('first',1,[card(1)],has_more=True,now=NOW)
        self.assertEqual(self.p.ingest_page('first',1,[card(2)],has_more=True,now=NOW)['reason'],'page_changed_rescan_required')
        self.assertEqual(self.p.ingest_page('first',2,[card(1)],has_more=False,now=NOW)['reason'],'repeated_page')

    def test_unknown_publication_retained_not_assigned_fake_latency(self):
        self.p.ingest_page('first',1,[card(1,None)],has_more=False,now=NOW)
        row=self.p.rows('listings')[0]
        self.assertEqual(row['stage'],'unresolved'); self.assertIsNone(row['published'])
        self.assertEqual(self.p.process_details(details,NOW),0)

    def test_source_retry_pause_and_terminal_audit(self):
        self.p.record_scan_failure('first','rate_limited',NOW,retry_after=20)
        self.assertEqual(self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW+1)['reason'],'source_backoff')
        self.p.record_scan_failure('first','network_error',NOW+20)
        self.p.record_scan_failure('first','network_error',NOW+40)
        self.assertEqual(self.p.rows('scans')[0]['state'],'unresolved')
        self.assertEqual(len(self.p.rows('events')),3)

    def test_transient_detail_retry_then_success(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        self.p.process_details(lambda row:(_ for _ in ()).throw(TimeoutError()),NOW)
        self.assertEqual(self.p.process_details(details,NOW+1),0)
        self.assertEqual(self.p.process_details(details,NOW+5),1)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'valuation')

    def test_detail_exhaustion_keeps_unresolved(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        for now in (1000,1005,1015):
            self.p.process_details(lambda row:{},now)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'unresolved')
        self.assertEqual(self.p.summary(1020)['oldest_unprocessed_age_seconds'],20)

    def test_unknown_valuation_retries_not_false_nonprofitable(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        self.p.process_details(details,NOW)
        self.p.process_valuations(lambda d,n:dict(status='unknown',reason='insufficient_peers'),NOW)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'valuation')
        self.p.process_valuations(estimate,NOW+5)
        self.assertEqual(len(self.p.rows('claims')),1)

    def test_valuation_exhaustion_terminal_unresolved_no_drop(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        self.p.process_details(details,NOW)
        for now in (1000,1005,1015):
            self.p.process_valuations(lambda d,n:dict(status='unknown',reason='insufficient_peers'),now)
        self.assertEqual(self.p.rows('listings')[0]['reason'],'insufficient_peers')
        self.assertEqual(self.p.rows('listings')[0]['stage'],'unresolved')
        self.assertEqual(self.p.rows('claims'),[])

    def test_unapproved_estimate_provenance_cannot_deliver(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        self.p.process_details(details,NOW)
        self.p.process_valuations(lambda d,n:{**estimate(d,n),'provenance':'paid_cache'},NOW)
        self.assertEqual(self.p.rows('claims'),[])

    def test_missing_fuel_gearbox_photo_and_damage_do_not_block(self):
        self.p.put_recipient(1,1,SearchFilter(fuels=frozenset({'diesel'}),transmissions=frozenset({'auto'})),started_at=900,access_until=2000)
        self.prepare(loader=lambda row:details(row,damaged=True,repair_parts=True))
        self.assertEqual(self.p.rows('claims')[0]['method'],'text')

    def test_known_optional_conflict_filters_and_separate_discount(self):
        self.p.put_recipient(1,1,SearchFilter(fuels=frozenset({'diesel'})),started_at=900,access_until=2000)
        self.p.put_recipient(2,2,SearchFilter(min_discount_percent=Decimal(40)),started_at=900,access_until=2000)
        self.prepare(loader=lambda row:details(row,fuel='petrol'))
        self.assertEqual(self.p.rows('claims'),[])
        self.assertIn('fuel_mismatch',{r['reason'] for r in self.p.rows('events')})
        self.assertIn('discount_mismatch',{r['reason'] for r in self.p.rows('events')})

    def test_multiple_searches_one_pair_no_duplicate_after_restart(self):
        self.p.put_recipient(1,2,SearchFilter(),started_at=900,access_until=2000)
        self.prepare(); self.p.send_due(self.accepted,NOW)
        self.p.close(); self.p=DurablePipeline(self.path); self.p.recover(NOW+1)
        self.assertEqual(self.p.send_due(self.accepted,NOW+1),0)
        self.assertEqual(len(self.p.rows('claims')),1)

    def test_stop_and_renewal_never_reenable_search(self):
        self.prepare(); self.p.stop_user(1); self.p.set_access(1,5000)
        self.assertEqual(self.p.send_due(self.accepted,NOW),0)
        self.assertEqual(self.p.rows('recipients')[0]['enabled'],0)

    def test_expiry_and_unknown_access_checked_at_each_send(self):
        self.add_user(2); self.add_user(3)
        self.prepare(); self.p.set_access(1,999); self.p.set_access(2,2000,known=False)
        calls=[]
        self.p.send_due(lambda c,d:(calls.append(c['user_id']) or self.accepted(c,d)),NOW)
        self.assertEqual(calls,[3])
        states={r['user_id']:r['state'] for r in self.p.rows('claims')}
        self.assertEqual(states,{1:'cancelled',2:'pending',3:'sent'})
        self.p.set_access(2,2000); self.p.send_due(self.accepted,NOW+5)
        self.assertEqual(self.p.summary(NOW+5)['claims']['sent'],2)

    def test_unknown_access_at_queue_build_retained_for_retry(self):
        self.p.set_access(1,2000,known=False); self.prepare()
        self.assertEqual(len(self.p.rows('claims')),1)
        self.p.set_access(1,2000); self.p.send_due(self.accepted,NOW)
        self.assertEqual(self.p.rows('claims')[0]['state'],'sent')

    def test_first_user_failure_does_not_block_other_users(self):
        self.add_user(2); self.prepare()
        self.p.send_due(lambda c,d:dict(outcome='rate_limited',retry_after=10) if c['user_id']==1 else self.accepted(c,d),NOW)
        states={r['user_id']:r['state'] for r in self.p.rows('claims')}
        self.assertEqual(states,{1:'pending',2:'sent'})
        self.assertEqual(self.p.send_due(self.accepted,NOW+1),0)
        self.p.send_due(self.accepted,NOW+10)
        self.assertEqual(self.p.summary(NOW+10)['claims']['sent'],2)

    def test_before_each_send_rechecks_stop_race(self):
        self.add_user(2); self.prepare()
        def sender(c,d):
            self.p.stop_user(2)
            return self.accepted(c,d)
        self.assertEqual(self.p.send_due(sender,NOW),1)
        self.assertEqual(self.p.rows('claims')[1]['state'],'cancelled')

    def test_photo_known_failure_falls_back_to_text(self):
        self.prepare(loader=lambda row:details(row,photo='https://example.invalid/photo.jpg'))
        self.p.send_due(lambda c,d:dict(outcome='photo_invalid'),NOW)
        self.assertEqual(self.p.rows('claims')[0]['method'],'text')
        self.p.send_due(self.accepted,NOW+5)
        self.assertEqual(self.p.rows('claims')[0]['state'],'sent')

    def test_timeout_is_uncertain_no_automatic_retry(self):
        self.prepare()
        self.p.send_due(lambda c,d:(_ for _ in ()).throw(TimeoutError()),NOW)
        self.assertEqual(self.p.rows('claims')[0]['state'],'uncertain')
        self.assertEqual(self.p.send_due(self.accepted,NOW+100),0)

    def test_restart_inflight_send_never_resends(self):
        self.prepare()
        self.p.db.execute("UPDATE claims SET state='sending'")
        self.p.recover(NOW+1)
        self.assertEqual(self.p.rows('claims')[0]['reason'],'restart_send_uncertain')
        self.assertEqual(self.p.send_due(self.accepted,NOW+100),0)

    def test_known_send_failure_exhaustion_remains_unresolved(self):
        self.prepare()
        for now in (1000,1005,1015):
            self.p.send_due(lambda c,d:dict(outcome='known_transient'),now)
        self.assertEqual(self.p.rows('claims')[0]['state'],'unresolved')

    def test_accepted_without_message_id_is_uncertain(self):
        self.prepare(); self.p.send_due(lambda c,d:dict(outcome='accepted'),NOW)
        self.assertEqual(self.p.rows('claims')[0]['state'],'uncertain')

    def test_actual_socket_attempt_in_loader_blocked(self):
        self.p.ingest_page('first',1,[card(1)],has_more=False,now=NOW)
        self.p.process_details(lambda row:socket.create_connection(('example.com',443)),NOW)
        self.assertEqual(self.p.summary(NOW)['outbound_guard']['blocked'],1)
        self.assertEqual(self.p.summary(NOW)['paid_calls'],0)

    def test_foreign_db_rejected(self):
        other=Path(self.tmp.name)/'foreign.free-pipeline.sqlite3'
        sqlite3.connect(other).close()
        with self.assertRaisesRegex(ValueError,'foreign_database'):
            DurablePipeline(other)


class GuardTests(unittest.TestCase):
    def test_paid_url_is_denied_and_counted_no_success(self):
        guard=OutboundGuard()
        with self.assertRaises(NetworkDenied):
            guard.check_url('https://developers.ria.com/auto/search?api_key=fixture')
        self.assertEqual((guard.paid_calls,guard.paid_blocked,guard.successful_external_calls),(0,1,0))

    def test_all_hosts_denied_including_telegram_dns(self):
        guard=OutboundGuard()
        with guard.isolated():
            with self.assertRaises(NetworkDenied):
                socket.getaddrinfo('api.telegram.org',443)
            with self.assertRaises(NetworkDenied):
                socket.socket()
        self.assertEqual(guard.blocked,2)

    def test_production_configuration_is_not_consumed(self):
        with patch.dict(os.environ,{'DATABASE_URL':'fixture-not-a-secret'}):
            with self.assertRaisesRegex(NetworkDenied,'production_configuration_present'):
                with OutboundGuard().isolated():
                    self.fail('must_not_enter')

class ExtendedDurabilityTests(unittest.TestCase):
    setUp=DurableTests.setUp
    tearDown=DurableTests.tearDown
    add_user=DurableTests.add_user
    prepare=DurableTests.prepare
    accepted=DurableTests.accepted
    def test_genuine_republication_checks_again_without_duplicate_pair(self):
        self.prepare(); self.p.send_due(self.accepted,NOW)
        self.p.put_recipient(2,2,SearchFilter(),started_at=1005,access_until=2000)
        self.p.start_scan('republished',1020,900)
        self.p.ingest_page('republished',1,[card(500,1010)],has_more=False,now=1020)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'detail')
        self.p.process_details(lambda row:{**details(row),'observed_at':1020},1020)
        self.p.process_valuations(estimate,1020); self.p.send_due(self.accepted,1020)
        self.assertEqual(self.p.summary(1020)['claims']['sent'],2)
        self.assertEqual(len(self.p.rows('listing_history')),1)
        self.assertEqual(self.p.summary(1020)['detail_calls'],2)

    def test_edit_timestamp_alone_does_not_republish_or_send_again(self):
        self.prepare(); self.p.send_due(self.accepted,NOW)
        self.p.start_scan('edited',1020,900)
        self.p.ingest_page('edited',1,[{**card(500),'updated_at':1010}],has_more=False,now=1020)
        self.assertEqual(self.p.rows('listing_history'),[])
        self.assertEqual(self.p.send_due(self.accepted,1020),0)

    def test_publication_proof_can_recover_from_unknown_later(self):
        self.p.ingest_page('first',1,[card(500,None)],has_more=False,now=NOW)
        self.p.start_scan('recovered',1020,900)
        self.p.ingest_page('recovered',1,[card(500)],has_more=False,now=1020)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'detail')
        self.assertEqual(len(self.p.rows('listing_history')),1)

    def test_old_claim_waits_for_republication_details_without_crashing(self):
        self.prepare()
        self.p.start_scan('republished',1020,900)
        self.p.ingest_page('republished',1,[card(500,1010)],has_more=False,now=1020)
        self.assertEqual(self.p.send_due(self.accepted,1020),0)
        self.assertEqual(self.p.rows('claims')[0]['state'],'pending')

    def test_stale_detail_cache_does_not_count_as_current_data(self):
        self.p.ingest_page('first',1,[card(500)],has_more=False,now=NOW)
        self.p.process_details(lambda row:{**details(row),'observed_at':900},NOW)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'detail')
        self.assertEqual(self.p.rows('claims'),[])

    def test_unknown_selected_region_remains_unresolved_pair(self):
        self.p.put_recipient(1,1,SearchFilter(regions=frozenset({'lviv'})),started_at=900,access_until=2000)
        self.prepare(loader=lambda row:{**details(row),'region':None})
        self.assertEqual(self.p.rows('claims')[0]['state'],'unresolved')
        self.assertEqual(self.p.rows('claims')[0]['reason'],'region_not_proven')
        self.assertEqual(self.p.send_due(self.accepted,NOW),0)

    def test_clock_rechecks_expiry_between_two_sends(self):
        self.add_user(2); self.prepare()
        times=iter((1000,2001))
        self.assertEqual(self.p.send_due(self.accepted,NOW,clock=lambda:next(times)),1)
        self.assertEqual(self.p.rows('claims')[1]['state'],'cancelled')
    def test_conflicting_current_usd_prices_are_unresolved_not_mixed(self):
        self.p.ingest_page('first',1,[card(500)],has_more=False,now=NOW)
        self.p.process_details(lambda row:{**details(row),'price':'12000'},NOW)
        self.assertEqual(self.p.rows('listings')[0]['stage'],'detail')
        self.assertEqual(self.p.rows('claims'),[])

    def test_visible_publication_conflict_is_not_overridden_by_card_date(self):
        self.prepare(loader=lambda row:{**details(row),'publication_proven':False})
        self.assertEqual(self.p.rows('claims')[0]['reason'],'publication_not_proven')
        self.assertEqual(self.p.rows('claims')[0]['state'],'unresolved')

    def test_suspicious_classification_never_qualifies_even_bad_estimator_status(self):
        self.p.ingest_page('first',1,[card(500)],has_more=False,now=NOW)
        self.p.process_details(details,NOW)
        self.p.process_valuations(lambda d,n:{**estimate(d,n),'classification':'suspicious_price'},NOW)
        self.assertEqual(self.p.rows('claims'),[])
    def test_parser_decimal_mileage_and_category_body_are_normalized(self):
        self.p.put_recipient(1,1,SearchFilter(mileage_max_km=150000,bodies=frozenset({'sedan'})),started_at=900,access_until=2000)
        self.prepare(loader=lambda row:{**details(row),'mileage_km':Decimal('100000'),'body':'Легкові'})
        self.assertEqual(len(self.p.rows('claims')),1)
        self.assertEqual(self.p.send_due(self.accepted,NOW),1)
