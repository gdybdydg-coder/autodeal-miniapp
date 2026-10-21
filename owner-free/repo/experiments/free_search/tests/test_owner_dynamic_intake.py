from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch, Mock
from experiments.free_search.owner_candidate_queue import CandidateQueue
from experiments.free_search.owner_dynamic_intake import DynamicIntakeState, transient_receipt
from experiments.free_search.owner_head_window import HeadWindow
from experiments.free_search.owner_stage_pipeline import StagePipeline
from experiments.free_search.owner_paid_coverage import classify
import test_owner_scan as fixtures
from test_owner_scan import page,cards,HEAD


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.root=Path(self.tmp.name)
        self.q=CandidateQueue(self.root/'q.db');self.s=DynamicIntakeState(self.q)
    def tearDown(self):self.q.close();self.tmp.cleanup()
    def accept(self, now, terminal=False):
        number=self.s.action(now,False)[1]
        meta=replace(page(number-1,terminal),url=self.s.url_for(number))
        self.s.accept_page(meta,[],now,{'url':meta.url,'ids':[]})
        return number
    def test_follows_beyond_six_interleaves_and_overlaps(self):
        walked=[self.accept(1000+i*20) for i in range(30)]
        self.assertGreater(max(walked),6)
        self.assertEqual(walked[:11],[1,2,1,3,1,2,1,4,1,5,1])
        self.assertGreater(walked.count(2),1)
        self.assertEqual(walked[::2],[1]*15)
    def test_links_are_observed_not_invented(self):
        with self.assertRaisesRegex(ValueError,'unobserved_pagination'):self.s.url_for(7)
        self.accept(100)
        self.assertEqual(self.s.url_for(2),HEAD+'?page=2')
    def test_restart_preserves_cursor_history_and_backoff(self):
        self.accept(100)
        e=dict(url=HEAD+'?page=2',status=200,error='UnicodeDecodeError')
        self.s.defer(e,110)
        before=self.s.recovery()
        self.q.close();self.q=CandidateQueue(self.root/'q.db');self.s=DynamicIntakeState(self.q)
        self.assertEqual(self.s.recovery(),before)
        self.assertEqual(self.s.action(120,True),('wait',140))
        self.assertEqual(self.s.action(141,True),('head',2))
    def test_terminal_restarts_head_then_new_cycle(self):
        self.accept(100);self.accept(120,True)
        self.assertEqual(self.accept(140),1)
        self.assertEqual(self.accept(160),2)
        self.assertEqual(self.q.db.execute('SELECT cycles FROM intake_dynamic').fetchone()[0],1)
    def test_later_404_returns_to_head_with_cooldown(self):
        self.accept(100)
        self.s.missing_head(2,120,{'status':404})
        self.assertEqual(self.accept(140),1)
        self.assertEqual(self.accept(160),1)
        self.assertEqual(self.accept(740),1)
        self.assertEqual(self.accept(760),2)
    def test_backoff_increases_caps_and_robots_does_not_clear_it(self):
        e=dict(url=HEAD+'?page=1',status=200,error='UnicodeDecodeError')
        delays=[]
        for i in range(12):
            self.s.defer(e,100+i*1000);delays.append(self.s.recovery()['retry_at']-(100+i*1000))
        self.assertEqual(delays[:3],[30,60,120]);self.assertEqual(max(delays),900)
        self.s.success({'url':'https://auto.ria.com/robots.txt'},12000)
        self.assertEqual(self.s.recovery()['failures'],12)
        self.s.success({'url':e['url']},13000)
        self.assertEqual(self.s.recovery()['failures'],0)
    def test_source_denial_challenge_and_unknown_error_never_retry(self):
        for status,error in [(403,'http_error'),(429,'http_error'),(200,'source_challenge'),
                             (200,'body_cap_reached'),(200,'unexpected_error'),(301,'http_error')]:
            self.assertFalse(transient_receipt(dict(status=status,error=error)))
        for status,error in [(503,'http_error'),(None,'TimeoutError'),(200,'UnicodeDecodeError')]:
            self.assertTrue(transient_receipt(dict(status=status,error=error)))
    def test_operator_stop_takes_precedence_over_retry(self):
        self.s.defer(dict(url=HEAD,status=503,error='http_error'),100)
        self.s.stop('operator',101)
        self.assertEqual(self.s.action(102,True),('stop',None))


class FirstObservationTests(unittest.TestCase):
    setUp=fixtures.ScanTests.setUp
    tearDown=fixtures.ScanTests.tearDown
    baseline=fixtures.ScanTests.baseline
    def test_single_page_sighting_enters_queue_and_baseline_is_still_blocked(self):
        self.baseline();window=HeadWindow(self.obs,dynamic=True)
        meta=replace(page(6),url=HEAD+'?page=7')
        window.accept(meta,cards(4,1),110)
        q=CandidateQueue(Path(self.tmp.name)/'q.db')
        p=StagePipeline(q,self.obs)
        self.assertEqual(p.ingest(cards(4,1),110),['4'])
        self.assertEqual(self.obs.signal('4',110)['reason'],'eligible')
        self.assertIsNone(self.obs.signal('4',110)['publication_at'])
        self.assertEqual(self.obs.signal('1',110)['reason'],'baseline_listing')
        self.assertEqual(p.ingest(cards(4,1),111),[])
        self.assertEqual(self.obs.signal('4',411)['reason'],'stale_observation')
        q.close()
    def test_wrong_scope_cannot_create_live_evidence(self):
        self.baseline();window=HeadWindow(self.obs,dynamic=True)
        with self.assertRaises(ValueError):window.accept(replace(page(6),scope_digest='wrong'),cards(4),110)
        self.assertEqual(self.obs.signal('4',110)['reason'],'not_observed')
    def test_empty_declared_end_is_not_fatal(self):
        self.baseline();window=HeadWindow(self.obs,dynamic=True)
        self.assertEqual(window.accept(page(6,True),[],110)['cards'],0)


class ReverseAuditTests(unittest.TestCase):
    def test_missing_paid_id_is_visible_without_seeding_queue(self):
        paid=dict(source_id='1',message_id=44,accepted_at=120,discovered_at=100)
        r=classify(paid,None,None,None,None,90)
        self.assertEqual(r['outcome'],'not_observed');self.assertTrue(r['within_current_collection'])
        self.assertFalse(classify(paid,None,None,None,None,110)['within_current_collection'])
    def test_both_sent_delta_and_observed_not_queued(self):
        paid=dict(source_id='1',message_id=44,accepted_at=120,discovered_at=100)
        obs=dict(first_seen=105,baseline=0)
        self.assertEqual(classify(paid,obs,None,None,None,90)['outcome'],'observed_not_queued')
        r=classify(paid,obs,None,None,dict(state='sent',accepted_at=115,message_id=43),90)
        self.assertEqual(r['outcome'],'both_sent');self.assertEqual(r['free_minus_paid_seconds'],-5)


class IntakeRecoveryIntegrationTests(unittest.TestCase):
    def test_unicode_failure_exits_batch_without_stopping_service_or_resetting_retry(self):
        import os
        from types import SimpleNamespace
        from experiments.free_search import owner_continuous_intake as m
        from experiments.free_search.owner_budget import Denied
        with TemporaryDirectory() as folder:
            root=Path(folder)
            fetch=Mock()
            def get(url,cap):
                return ('User-agent: *' if url==m.ROBOTS else None,
                        dict(url=url,status=200,error=None if url==m.ROBOTS else 'UnicodeDecodeError',completed_at=100))
            fetch.get.side_effect=get
            with patch.dict(os.environ,{},clear=True), patch.object(m,'WORKSPACE',root), \
                 patch.object(m,'Coordinator'), patch.object(m,'PublicFetch',return_value=fetch), \
                 patch.object(m,'wait_for_fetch') as wait, \
                 patch.object(m,'time',SimpleNamespace(time=lambda:100.,sleep=lambda _:None)):
                report=m.run_owned('synthetic_token',150)
                self.assertEqual(report['reason'],'source_retry_wait')
                self.assertEqual(fetch.get.call_count,2)
                q=CandidateQueue(root/'night21-candidates.sqlite3')
                s=DynamicIntakeState(q,pages=3)
                self.assertEqual(s.recovery()['retry_at'],130)
                self.assertEqual(q.db.execute('SELECT stopped FROM intake_control').fetchone()[0],0)
                q.close()
                wait.side_effect=Denied('batch_deadline')
                report=m.run_owned('synthetic_token',150)
                self.assertEqual(report['reason'],'batch_deadline')
                self.assertEqual(fetch.get.call_count,2)
