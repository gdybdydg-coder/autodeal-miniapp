from dataclasses import replace
import unittest
import test_owner_scan as fixtures
from test_owner_scan import page,cards,HEAD
from experiments.free_search.owner_head_window import HeadWindow

class HeadTests(unittest.TestCase):
    setUp=fixtures.ScanTests.setUp
    tearDown=fixtures.ScanTests.tearDown
    baseline=fixtures.ScanTests.baseline
    def test_repeated_head_confirms_id_without_claiming_complete_scan(self):
        self.baseline()
        window=HeadWindow(self.obs)
        meta=replace(page(),url=HEAD+'?page=1')
        result=window.accept(meta,cards(4,1),110)
        self.assertIsNone(result['whole_market_recall'])
        self.assertEqual(self.obs.signal('4',110)['reason'],'scan_not_completed')
        window.accept(meta,cards(4,1),115)
        self.assertEqual(self.obs.signal('4',115)['reason'],'eligible')
        self.assertEqual(self.obs.signal('1',115)['reason'],'baseline_listing')
        self.assertEqual(self.obs.signal('4',411)['reason'],'stale_observation')

    def test_scope_failure_and_same_time_do_not_confirm(self):
        self.baseline();window=HeadWindow(self.obs)
        meta=replace(page(),url=HEAD+'?page=1')
        window.accept(meta,cards(4,1),110)
        with self.assertRaises(ValueError):window.accept(meta,cards(4,1),110)
        with self.assertRaises(ValueError):window.accept(replace(meta,scope_digest='wrong'),cards(4,1),111)
        self.assertEqual(self.obs.signal('4',111)['reason'],'scan_not_completed')

    def test_later_window_pages_confirm_but_outside_window_rejected(self):
        self.baseline();window=HeadWindow(self.obs)
        window.accept(page(1),cards(4,1),110)
        window.accept(page(2),cards(4,1),115)
        self.assertEqual(self.obs.signal('4',115)['reason'],'eligible')
        window.accept(page(5),cards(5,1),116)
        with self.assertRaises(ValueError):window.accept(page(6),cards(6,1),117)

    def test_explicit_review_window_preserves_first_seen_and_default_expiry(self):
        self.baseline();window=HeadWindow(self.obs)
        window.accept(page(1),cards(4,1),110);window.accept(page(1),cards(4,1),115)
        self.assertEqual(self.obs.signal('4',450)['reason'],'stale_observation')
        reviewed=self.obs.signal('4',450,max_age=900)
        self.assertEqual(reviewed['reason'],'eligible');self.assertEqual(reviewed['first_seen'],110)
        self.assertEqual(self.obs.signal('4',1011,max_age=900)['reason'],'stale_observation')
        with self.assertRaises(ValueError):self.obs.signal('4',450,max_age=901)
