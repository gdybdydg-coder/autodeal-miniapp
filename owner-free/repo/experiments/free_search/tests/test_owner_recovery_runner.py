import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from dataclasses import replace
from contextlib import ExitStack
from experiments.free_search.owner_budget import Budget
from experiments.free_search.owner_night_runtime import Coordinator
from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_scan import ScanCursor,HEAD
from experiments.free_search.owner_night_scan import run
import test_owner_scan as fixtures


class RecoveryRunnerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.lock=self.root/'lock.sqlite3'
        lock=Coordinator(self.lock);lock.acquire('fixture_runner',100);lock.close()
        self.path=self.root/'night-20261020.owner-observations.sqlite3'
        obs=Observations(self.path);scan=ScanCursor(obs)
        self.head=replace(fixtures.page(),url=HEAD+'?page=1')
        scan.start(100,head_url=self.head.url)
        scan.accept(self.head,fixtures.cards(1,2),101)
        scan.accept(fixtures.page(1,True),fixtures.cards(3),102)
        scan.accept(self.head,fixtures.cards(4,1),103)
        obs.close()
        stack=ExitStack();self.addCleanup(stack.close)
        prefix='experiments.free_search.owner_night_scan.'
        stack.enter_context(patch(prefix+'WORKSPACE',self.root))
        stack.enter_context(patch(prefix+'LOCK',self.lock))
        stack.enter_context(patch(prefix+'time.time',return_value=110))
        self.fetch=stack.enter_context(patch(prefix+'PublicFetch'))
        self.fetch.return_value.get.return_value=('fixture',{'completed_at':110})
        self.meta=stack.enter_context(patch(prefix+'inspect_page'))
        self.cards=stack.enter_context(patch(prefix+'parse_public_cards'))

    def invoke(self,count=2,recover=True):
        return run('fixture_runner',count,budget_factory=lambda:Budget(
            self.root/'ledger.sqlite3',40,100000000),head_url=self.head.url,
            recover_moving=recover)

    def test_recovery_is_explicit_no_default_fetch(self):
        result=self.invoke(recover=False)
        self.assertEqual(result['status'],'persisted_scan_block_no_retry')
        self.fetch.assert_not_called()

    def test_runner_recovers_resumes_and_completes_without_baseline_signal(self):
        self.meta.side_effect=[self.head,fixtures.page(1,True),self.head]
        self.cards.side_effect=[fixtures.cards(4,1),fixtures.cards(2,3),fixtures.cards(4,1)]
        first=self.invoke()
        self.assertIn('recovery',first)
        self.assertEqual(first['scan']['phase'],'head_check')
        self.assertEqual(first['scan']['expected'],self.head.url)
        second=self.invoke(1)
        self.assertEqual(second['status'],'complete')
        self.assertNotIn('recovery',second)
        obs=Observations(self.path)
        try:
            self.assertEqual(obs.signal('4',111)['reason'],'baseline_listing')
            self.assertEqual(obs.db.execute('SELECT count(*) FROM observations').fetchone()[0],4)
            self.assertEqual(obs.db.execute('SELECT count(*) FROM scan_recovery').fetchone()[0],1)
        finally:obs.close()

    def test_scope_block_cannot_trigger_network_even_with_recovery_enabled(self):
        obs=Observations(self.path)
        obs.db.execute("UPDATE scans SET reason='scope_metadata_changed' WHERE id=1")
        obs.close()
        self.assertEqual(self.invoke()['status'],'persisted_scan_block_no_retry')
        self.fetch.assert_not_called()

    def test_interrupted_response_retains_recovered_cursor_without_second_retry(self):
        self.fetch.return_value.get.side_effect=[('robots',{'completed_at':110}),
                                                (None,{'completed_at':110,'error':'incomplete_response'})]
        result=self.invoke()
        self.assertEqual(result['status'],'source_unavailable_cursor_retained')
        self.assertEqual(result['scan']['expected'],self.head.url)
        self.assertEqual(self.fetch.return_value.get.call_count,2)
