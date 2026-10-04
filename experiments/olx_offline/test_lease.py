import unittest
from experiments.olx_offline.lease import claim,owns,release
from experiments.olx_offline.run_guard import HARD_STOP
from experiments.olx_offline.test_pipeline import NOW


class LeaseTests(unittest.TestCase):
    def test_active_lease_and_expired_owner(self):
        original={'stop':False,'lease':None}
        active=claim(original,'synthetic-owner-1',NOW)
        self.assertIsNone(original['lease'])
        with self.assertRaises(ValueError):claim(active,'synthetic-owner-2',NOW+1)
        replacement=claim(active,'synthetic-owner-2',NOW+1800)
        self.assertFalse(owns(replacement,'synthetic-owner-1',NOW+1800))
        self.assertTrue(owns(replacement,'synthetic-owner-2',NOW+1800))
        with self.assertRaises(ValueError):release(replacement,'synthetic-owner-1')
        self.assertIsNone(release(replacement,'synthetic-owner-2')['lease'])

    def test_stop_and_absolute_deadline(self):
        with self.assertRaises(ValueError):claim({'stop':True},'synthetic-owner-1',NOW)
        with self.assertRaises(ValueError):claim({'stop':False},'synthetic-owner-1',int(HARD_STOP))
        active=claim({'stop':False},'synthetic-owner-1',int(HARD_STOP)-5)
        self.assertEqual(active['lease']['expires_at'],int(HARD_STOP))
        self.assertFalse(owns(active,'synthetic-owner-1',int(HARD_STOP)))
