from unittest import TestCase
from experiments.free_search.owner_source_timing import SourceTiming


class SourceTimingTests(TestCase):
    def setUp(self):
        self.log=SourceTiming(':memory:');self.addCleanup(self.log.close)

    def test_first_observed_immutable_and_missing_paid_unknown(self):
        self.log.observe(111,'123',4,'epoch','free',100)
        self.log.observe(111,'123',4,'epoch','free',120)
        r=self.log.compare(111,'123',4,'epoch')
        self.assertEqual(r['sources']['free']['first_seen'],100)
        self.assertIsNone(r['free_minus_paid_discovery_seconds'])
        self.assertIsNone(r['publication_at'])

    def test_two_source_receipts_and_signed_difference(self):
        for source,seen,accepted,message in [('free',100,112,1),('paid',105,109,2)]:
            self.log.observe(111,'123',4,'epoch',source,seen)
            self.log.receipt(111,'123',4,'epoch',source,accepted,message)
        r=self.log.compare(111,'123',4,'epoch')
        self.assertEqual(r['free_minus_paid_discovery_seconds'],-5)
        self.assertEqual(r['free_minus_paid_acceptance_seconds'],3)

    def test_different_owner_search_epoch_never_compared(self):
        self.log.observe(111,'123',4,'epoch','free',100)
        for owner,search,epoch in [(222,4,'epoch'),(111,5,'epoch'),(111,4,'new')]:
            self.log.observe(owner,'123',search,epoch,'paid',105)
        self.assertIsNone(self.log.compare(111,'123',4,'epoch')['free_minus_paid_discovery_seconds'])

    def test_receipts_no_overwrite_or_reversed_time(self):
        self.log.observe(111,'123',4,'epoch','free',100)
        with self.assertRaises(ValueError):self.log.receipt(111,'123',4,'epoch','free',99,1)
        self.log.receipt(111,'123',4,'epoch','free',110,1)
        with self.assertRaises(ValueError):self.log.receipt(111,'123',4,'epoch','free',120,2)
        self.assertEqual(self.log.compare(111,'123',4,'epoch')['sources']['free']['message_id'],1)
