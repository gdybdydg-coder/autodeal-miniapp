from pathlib import Path
import sqlite3
import tempfile
import unittest

from experiments.free_search.owner_observations import Observations
from experiments.free_search.owner_scan import ScanCursor, HEAD
from experiments.free_search.public_cards import PublicCard
from experiments.free_search.public_pagination import inspect_page


def page(index=0, terminal=False, scope='lang_id=4&top=1', size=2):
    url = HEAD if index == 0 else HEAD + f'?page={index+1}'
    nav = ("<a class='page-link js-next disabled'></a>" if terminal else
           f"<a class='page-link js-next' href='{HEAD}?page={index+2}'></a>")
    html = (f"<script data-search-query='page={index}&countpage={size}&{scope}'></script>"
            f"<div id='pagination'>{nav}</div>")
    return inspect_page(html, url)


def cards(*ids):
    return [PublicCard(str(i), f'https://auto.ria.com/uk/auto_fixture_{i}.html',
                       50, None, None, False, (), ()) for i in ids]


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'scan.owner-observations.sqlite3'
        self.obs = Observations(self.path)
        self.scan = ScanCursor(self.obs)

    def tearDown(self):
        self.obs.close()
        self.tmp.cleanup()

    def baseline(self):
        self.scan.start(100)
        self.scan.accept(page(), cards(1, 2), 101)
        self.scan.accept(page(1, True), cards(3), 102)
        self.assertEqual(self.scan.accept(page(), cards(1, 2), 103)['phase'], 'complete')

    def test_missing_page_restart_preserves_baseline_and_is_bounded(self):
        self.scan.start(100)
        self.scan.accept(page(),cards(1,2),101)
        for at in (102,105):
            self.scan.block_source()
            receipt=dict(status=404,error='http_error',url=self.scan.status()['expected'],completed_at=at)
            self.scan.recover_missing_page(at+1,receipt)
            self.scan.accept(page(),cards(1,2),at+2)
        self.scan.block_source()
        receipt=dict(status=404,error='http_error',url=self.scan.status()['expected'],completed_at=108)
        with self.assertRaisesRegex(ValueError,'recovery_limit'):
            self.scan.recover_missing_page(109,receipt)
        self.assertEqual(self.obs.signal('1',109)['reason'],'baseline_listing')

    def test_missing_page_does_not_recover_denials_or_wrong_url(self):
        self.scan.start(100);self.scan.accept(page(),cards(1,2),101);self.scan.block_source()
        for status,url in ((403,HEAD+'?page=2'),(429,HEAD+'?page=2'),(404,HEAD)):
            with self.assertRaisesRegex(ValueError,'evidence_required'):
                self.scan.recover_missing_page(103,dict(status=status,url=url,error='http_error',completed_at=102))

    def test_all_initial_pages_are_baseline_across_restart(self):
        self.scan.start(100)
        self.scan.accept(page(), cards(1, 2), 101)
        self.obs.close()
        self.obs = Observations(self.path)
        self.scan = ScanCursor(self.obs)
        self.assertEqual(self.scan.status()['expected'], HEAD + '?page=2')
        with self.assertRaisesRegex(ValueError, 'requires_resume'):
            self.scan.start(102)
        self.scan.accept(page(1, True), cards(3), 103)
        self.scan.accept(page(), cards(1, 2), 104)
        for ident in ('1', '2', '3'):
            self.assertEqual(self.scan.signal(ident, 105)['reason'], 'baseline_listing')

    def test_new_signal_requires_complete_scan_and_is_not_publication(self):
        self.baseline()
        self.scan.start(110)
        self.scan.accept(page(), cards(4, 1), 111)
        self.assertEqual(self.scan.signal('4', 112)['reason'], 'scan_not_completed')
        self.assertEqual(self.obs.signal('4', 112)['reason'], 'scan_not_completed')
        self.scan.accept(page(1, True), cards(2, 3), 112)
        self.scan.accept(page(), cards(4, 1), 113)
        result = self.scan.signal('4', 114)
        self.assertEqual(result['reason'], 'eligible')
        self.assertIsNone(result['publication_at'])
        self.assertEqual(result['first_seen'], 111)

    def test_scope_change_stops_cursor_and_does_not_ingest_page(self):
        self.scan.start(100)
        self.scan.accept(page(), cards(1, 2), 101)
        result = self.scan.accept(page(1, True, scope='lang_id=4&top=1&abroad=2'), cards(3), 102)
        self.assertEqual(result['reason'], 'scope_metadata_changed')
        self.assertEqual(result['expected'], HEAD + '?page=2')
        self.assertEqual(self.obs.signal('3', 103)['reason'], 'not_observed')

    def test_changed_head_requires_explicit_recovery_and_keeps_baseline(self):
        self.scan.start(100)
        self.scan.accept(page(), cards(1, 2), 101)
        self.scan.accept(page(1, True), cards(3), 102)
        result = self.scan.accept(page(), cards(4, 1), 103)
        self.assertEqual(result['reason'], 'head_changed_recovery_required')
        self.scan.abandon()
        self.scan.start(104)
        self.scan.accept(page(), cards(4, 1), 105)
        self.scan.accept(page(1, True), cards(2, 3), 106)
        self.scan.accept(page(), cards(4, 1), 107)
        self.assertEqual(self.scan.signal('4', 108)['reason'], 'baseline_listing')

    def test_capacity_failure_rolls_back_observations_and_cursor_together(self):
        self.obs.close()
        self.path = Path(self.tmp.name) / 'small.owner-observations.sqlite3'
        self.obs = Observations(self.path, capacity=3)
        self.scan = ScanCursor(self.obs)
        self.scan.start(100)
        self.scan.accept(page(), cards(1, 2), 101)
        before = self.scan.status()
        with self.assertRaisesRegex(ValueError, 'observation_capacity'):
            self.scan.accept(page(1, True), cards(3, 4), 102)
        self.assertEqual(self.scan.status(), before)
        self.assertEqual(self.obs.signal('3', 103)['reason'], 'not_observed')

    def test_baseline_migration_suppresses_legacy_first_seen(self):
        self.obs.ingest(cards(1), 90)
        self.obs.ingest(cards(2), 91)
        self.assertEqual(self.obs.signal('2', 92)['reason'], 'scan_not_completed')
        self.baseline()
        self.assertEqual(self.scan.signal('2', 104)['reason'], 'baseline_listing')

    def test_later_scope_change_requires_new_explicit_design_not_new_signals(self):
        self.baseline()
        self.scan.start(110)
        result = self.scan.accept(page(scope='lang_id=4&top=0'), cards(9), 111)
        self.assertEqual(result['reason'], 'scope_metadata_changed')
        self.assertEqual(self.scan.signal('9', 112)['reason'], 'not_observed')

    def test_page_gap_and_short_nonterminal_page_do_not_complete(self):
        self.scan.start(100)
        self.scan.accept(page(), cards(1), 101)
        self.assertEqual(self.scan.status()['phase'], 'pages')
        result = self.scan.accept(page(2, True), cards(3), 102)
        self.assertEqual(result['reason'], 'unexpected_page')

    def test_invalid_cards_and_clock_leave_cursor_unchanged(self):
        self.scan.start(100)
        before = self.scan.status()
        for values, now in ((cards(1, 1), 101), (cards(1), 99)):
            with self.assertRaises(ValueError):
                self.scan.accept(page(), values, now)
            self.assertEqual(self.scan.status(), before)

    def test_cursor_write_failure_rolls_back_successful_ingest(self):
        self.scan.start(100)
        before = self.scan.status()
        self.obs.db.execute("CREATE TRIGGER fail_cursor BEFORE UPDATE ON scans "
                            "BEGIN SELECT RAISE(ABORT, 'simulated_disk_failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.scan.accept(page(), cards(1, 2), 101)
        self.assertEqual(self.scan.status(), before)
        self.assertEqual(self.obs.signal('1', 102)['reason'], 'not_observed')
        self.assertEqual(self.obs.db.execute('SELECT count(*) FROM scan_seen').fetchone()[0], 0)
