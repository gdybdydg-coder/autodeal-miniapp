from unittest import TestCase
from tempfile import TemporaryDirectory
from experiments.free_search.owner_budget import Budget, Denied, public_url

ROBOTS = 'https://auto.ria.com/robots.txt'
PAGE = 'https://auto.ria.com/uk/last/hour/'


class BudgetTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + '/ledger.sqlite'
        self.b = Budget(self.path, 3, 3000); self.addCleanup(self.b.close)
        self.b.observe(ROBOTS); self.b.observe(PAGE)

    def robots(self):
        i = self.b.reserve(ROBOTS, 1000, 100)
        self.b.complete(i, 101, 200, 500, robots_valid=True)

    def test_public_only(self):
        for url in ('https://developers.ria.com/auto/search', 'https://auto.ria.com/graphql',
                    'https://auto.ria.com/api/search', 'https://auto.ria.com@evil.test/robots.txt',
                    ROBOTS+'?key=secret', ROBOTS+'#fragment'):
            with self.subTest(url=url), self.assertRaises(Denied): public_url(url)

    def test_observation_and_robots_required(self):
        for url in (PAGE, 'https://auto.ria.com/uk/auto_example_123.html'):
            with self.assertRaises(Denied): self.b.reserve(url, 1000, 100, True)
        self.assertEqual(self.b.summary()['reserved_gets'], 0)

    def test_pause_after_completion(self):
        self.robots()
        with self.assertRaisesRegex(Denied, 'completion_pause'): self.b.reserve(PAGE,1000,104,True)
        self.b.reserve(PAGE,1000,105,True)

    def test_crash_reservation_retained_across_connections(self):
        self.b.reserve(ROBOTS,1000,100)
        other = Budget(self.path,3,3000); self.addCleanup(other.close)
        with self.assertRaisesRegex(Denied,'crashed'): other.reserve(ROBOTS,1000,1000)
        self.assertEqual(other.summary()['reserved_bytes'],1000)

    def test_hold_persists(self):
        i = self.b.reserve(ROBOTS,1000,100); self.b.complete(i,101,403,0)
        other = Budget(self.path,3,3000); self.addCleanup(other.close)
        with self.assertRaisesRegex(Denied,'persistent_source_hold'): other.reserve(ROBOTS,1000,105)

    def test_challenge_with_http_200_holds(self):
        i = self.b.reserve(ROBOTS,1000,100); self.b.complete(i,101,200,500,challenge=True,robots_valid=True)
        self.assertTrue(self.b.summary()['source_hold'])

    def test_byte_and_get_budget_no_refund(self):
        self.robots()
        i=self.b.reserve(PAGE,1500,105,True); self.b.complete(i,106,500,0)
        with self.assertRaisesRegex(Denied,'budget_exhausted'): self.b.reserve(PAGE,501,110,True)
        i=self.b.reserve(PAGE,500,110,True); self.b.complete(i,111,None,0)
        with self.assertRaisesRegex(Denied,'budget_exhausted'): self.b.reserve(PAGE,1,115,True)
        self.assertEqual(self.b.summary()['reserved_bytes'],3000)

    def test_limits_cannot_be_increased_on_reopen(self):
        with self.assertRaisesRegex(Denied,'persistent_limits_differ'): Budget(self.path,4,4000)

    def test_robots_expiry(self):
        self.robots()
        with self.assertRaisesRegex(Denied,'robots_required'): self.b.reserve(PAGE,100,3701,True)

    def test_completion_cannot_clear_hold_or_refund(self):
        i=self.b.reserve(ROBOTS,1000,100); self.b.complete(i,101,429,0)
        with self.assertRaises(Denied): self.b.complete(i,102,200,10,robots_valid=True)
        self.assertTrue(self.b.summary()['source_hold'])

    def test_invalid_clock_or_cap_never_reserves(self):
        for cap,now in ((0,100),(2500001,100),(100,float('nan')),(True,100)):
            with self.assertRaises(Denied): self.b.reserve(ROBOTS,cap,now)
        self.assertEqual(self.b.summary()['reserved_gets'],0)
