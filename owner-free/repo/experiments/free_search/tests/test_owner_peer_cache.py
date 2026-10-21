import tempfile
from pathlib import Path
import unittest
from experiments.free_search.owner_peer_cache import PeerCache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'cache.owner-peers.sqlite3'

    def test_restart_freshness_and_failure_supersedes_old_success(self):
        cache = PeerCache(self.path)
        cache.put('1', 100, row={'listing_id':'1', 'observed_at':100, 'price':'10000'})
        cache.close()
        cache = PeerCache(self.path)
        self.addCleanup(cache.close)
        self.assertEqual(len(cache.snapshot(110)['rows']), 1)
        self.assertEqual(cache.snapshot(86501)['unavailable']['1'], 'stale_public_record')
        cache.put('1', 120, reason='currency_not_crosschecked')
        self.assertEqual(cache.snapshot(121)['rows'], [])
        self.assertEqual(cache.snapshot(121)['unavailable']['1'], 'currency_not_crosschecked')

    def test_conflicts_and_private_fields_rejected(self):
        cache = PeerCache(self.path)
        self.addCleanup(cache.close)
        cache.put('1', 100, reason='missing_price')
        for observed, reason in ((99,'missing_price'),(100,'price_conflict')):
            with self.assertRaises(ValueError):
                cache.put('1', observed, reason=reason)
        with self.assertRaisesRegex(ValueError, 'invalid_public_record'):
            cache.put('2', 100, row={'listing_id':'2','observed_at':100,'seller_phone':'secret'})
        self.assertEqual(cache.snapshot(101)['unavailable'], {'1':'missing_price'})

    def test_capacity_failure_keeps_existing_record(self):
        cache = PeerCache(self.path, capacity=1)
        self.addCleanup(cache.close)
        cache.put('1', 100, reason='unknown')
        with self.assertRaisesRegex(ValueError, 'cache_capacity'):
            cache.put('2', 101, reason='unknown')
        self.assertEqual(len(cache.snapshot(102)['unavailable']), 1)
