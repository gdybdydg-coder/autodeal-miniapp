from unittest import TestCase
from dataclasses import replace
import test_owner_pilot as fixtures
import test_owner_stage_pipeline as stage_fixtures
from experiments.free_search.owner_queue_dispatch import dispatch


class QueueDispatchTests(TestCase):
    setUp=stage_fixtures.StagePipelineTests.setUp

    def run_dispatch(self,fetch,**kwargs):
        return dispatch(self.pipeline,fetch,clock=lambda:self.now,deadline=self.now+100,**kwargs)

    def test_restart_does_not_refetch_detail_waiting_for_browser(self):
        calls=[]
        def fetch(*args):calls.append(args);return self.page
        self.assertEqual(len(self.run_dispatch(fetch)['details']),1)
        self.assertEqual(self.run_dispatch(fetch)['details'],[])
        self.assertEqual(len(calls),1)

    def test_waiting_browser_does_not_starve_new_detail(self):
        self.pipeline.detail(self.page,self.now)
        second=fixtures.page('12345679')
        card=replace(self.card,listing_id=second.listing_id,url=second.url)
        self.obs.ingest([card],self.now)
        self.pipeline.ingest([card],self.now)
        report=self.run_dispatch(lambda *a:second,limit=1)
        self.assertEqual(report['details'][0]['id'],'12345679')

    def test_expired_backlog_consumes_no_network(self):
        self.now+=301
        report=self.run_dispatch(lambda *a:self.fail('stale fetch'))
        self.assertEqual(report['expired'],['12345678'])
        self.assertEqual(report['details'],[])

    def test_source_failure_is_terminal_without_retry(self):
        def denied(*args):raise ValueError('source_challenge')
        r=self.run_dispatch(denied)
        self.assertEqual(r['stop'],'source_unavailable_no_retry')
        self.assertEqual(self.queue.get('12345678')['stage'],'rejected')
        self.assertEqual(self.run_dispatch(lambda *a:self.fail('retry'))['details'],[])

    def test_non_passenger_does_not_enter_browser_queue(self):
        page=replace(self.page,html=self.page.html.replace('Легкові з пробігом','Вантажівки'))
        r=self.run_dispatch(lambda *a:page)
        self.assertEqual(r['rejected'][0]['reason'],'passenger_category_not_proven')
        self.assertEqual(self.queue.pending(stage='detail'),[])

    def test_invalid_head_window_size_rejected_before_network(self):
        from experiments.free_search.owner_head_probe import run
        for value in (0,7,True):
            with self.assertRaisesRegex(ValueError,'invalid_head_page_count'):run('unused',pages=value)

    def test_deadline_stops_before_fetch_and_wrong_identity_never_persists(self):
        r=dispatch(self.pipeline,lambda *a:self.fail('deadline fetch'),clock=lambda:self.now,deadline=self.now)
        self.assertEqual(r['stop'],'deadline')
        with self.assertRaisesRegex(ValueError,'dispatcher_fetch_identity_mismatch'):
            self.run_dispatch(lambda *a:fixtures.page('999'))
        self.assertEqual(self.queue.get('12345678')['stage'],'discovered')

