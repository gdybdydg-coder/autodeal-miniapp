import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from dataclasses import replace
from contextlib import ExitStack
from experiments.free_search.owner_night_runtime import Coordinator
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_scan import ScanCursor, HEAD
from experiments.free_search.owner_live_scan import run
import test_owner_scan as fixtures


class LiveScanTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.lock=self.root/'lock.sqlite3'
        lock=Coordinator(self.lock);lock.acquire('fixture_live',100);lock.close()
        self.path=self.root/'night-20261020.owner-observations.sqlite3'
        obs=Observations(self.path);scan=ScanCursor(obs)
        scan.start(100);scan.accept(fixtures.page(),fixtures.cards(1,2),101)
        scan.accept(fixtures.page(1,True,scope='custom=1'),fixtures.cards(3),102);obs.close()
        self.head=replace(fixtures.page(scope='custom=1'),url=HEAD+'?page=1')
        stack=ExitStack();self.addCleanup(stack.close);prefix='experiments.free_search.owner_live_scan.'
        for key,value in [('WORKSPACE',self.root),('LOCK',self.lock)]:stack.enter_context(patch(prefix+key,value))
        stack.enter_context(patch(prefix+'time.time',return_value=110))
        self.sleep=stack.enter_context(patch(prefix+'time.sleep'))
        self.fetch=stack.enter_context(patch(prefix+'PublicFetch'))
        self.fetch.return_value.get.return_value=('fixture',{'completed_at':110})
        self.meta=stack.enter_context(patch(prefix+'inspect_page'))
        self.cards=stack.enter_context(patch(prefix+'parse_public_cards'))

    def test_block_has_no_requests_without_explicit_transition(self):
        self.assertEqual(run('fixture_live')['reason'],'persisted_scan_block')
        self.fetch.return_value.get.assert_not_called()

    def test_transition_pages_resume_to_baseline_complete(self):
        self.meta.side_effect=[self.head,self.head,fixtures.page(1,True,scope='custom=1'),self.head]
        self.cards.side_effect=[fixtures.cards(2,3),fixtures.cards(4),fixtures.cards(2,3)]
        a=run('fixture_live',transition=True,max_pages=2)
        self.assertEqual(a['scan']['phase'],'head_check')
        b=run('fixture_live',max_pages=1)
        self.assertEqual(b['status'],'complete')
        obs=Observations(self.path)
        try:self.assertEqual(obs.signal('3',110)['reason'],'baseline_listing')
        finally:obs.close()

    def test_source_failure_never_retries_or_abandons_original(self):
        self.fetch.return_value.get.side_effect=[('robots',{'completed_at':110}),
            (None,{'completed_at':110,'error':'fixture'})]
        r=run('fixture_live',transition=True)
        self.assertEqual(r['reason'],'source_unavailable_no_retry')
        self.assertEqual(r['scan']['id'],1)
        self.assertEqual(self.fetch.return_value.get.call_count,2)

    def test_wait_includes_margin_before_source_reservation(self):
        self.fetch.return_value.get.return_value=(None,{'completed_at':110})
        run('fixture_live',transition=True)
        self.assertGreaterEqual(self.sleep.call_args.args[0],0.05)

    def test_source_failure_on_active_scan_persists_across_runs(self):
        self.meta.side_effect=[self.head,self.head]
        self.cards.return_value=fixtures.cards(2,3)
        run('fixture_live',transition=True,max_pages=1)
        self.fetch.return_value.get.return_value=(None,{'completed_at':110})
        result=run('fixture_live')
        self.assertEqual(result['scan']['reason'],'source_unavailable_no_retry')
        self.fetch.return_value.get.reset_mock()
        self.assertEqual(run('fixture_live')['reason'],'persisted_scan_block')
        self.fetch.return_value.get.assert_not_called()
