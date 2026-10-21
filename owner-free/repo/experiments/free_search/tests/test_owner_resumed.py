import http.client
import unittest
from unittest.mock import MagicMock
from dataclasses import replace
from experiments.free_search.owner_public_fetch import PublicFetch
from experiments.free_search.owner_scan import HEAD, ScanCursor
from experiments.free_search.owner_observations import Observations
import test_owner_scan as fixtures


class ResumedScanTests(unittest.TestCase):
    setUp = fixtures.ScanTests.setUp
    tearDown = fixtures.ScanTests.tearDown
    def test_canonical_head_persists_across_restart_and_head_check(self):
        first=replace(fixtures.page(),url=HEAD+'?page=1')
        self.scan.start(100,head_url=first.url)
        self.scan.accept(first,fixtures.cards(1,2),101)
        self.obs.close()
        self.obs=Observations(self.path);self.scan=ScanCursor(self.obs)
        self.scan.accept(fixtures.page(1,True),fixtures.cards(3),102)
        self.assertEqual(self.scan.status()['expected'],first.url)
        self.assertEqual(self.scan.accept(first,fixtures.cards(1,2),103)['phase'],'complete')
        self.assertEqual(self.scan.signal('3',104)['reason'],'baseline_listing')

    def test_invented_head_denied(self):
        with self.assertRaisesRegex(ValueError,'unsupported_scan_head'):
            self.scan.start(100,head_url=HEAD+'?page=1&custom=1')


class PartialTransportTests(unittest.TestCase):
    def test_partial_bytes_accounted_and_never_parsed_or_retried(self):
        budget=MagicMock();budget.reserve.return_value=9
        fetch=PublicFetch(budget)
        response=MagicMock();response.status=200
        response.read.side_effect=http.client.IncompleteRead(b'partial',100)
        fetch.opener=MagicMock()
        fetch.opener.open.return_value.__enter__.return_value=response
        body,receipt=fetch.get(HEAD)
        self.assertIsNone(body)
        self.assertEqual(receipt['error'],'incomplete_response')
        self.assertEqual(receipt['bytes'],7)
        self.assertEqual(budget.complete.call_args.args[3],7)
        self.assertEqual(fetch.opener.open.call_count,1)
