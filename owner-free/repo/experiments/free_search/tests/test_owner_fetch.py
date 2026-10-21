from email.message import Message
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from experiments.free_search.owner_budget import Budget, Denied
from experiments.free_search.owner_public_fetch import PublicFetch, ROBOTS

PAGE = 'https://auto.ria.com/uk/last/hour/'


class FetchTests(TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.b=Budget(tmp.name+'/budget',5,10000); self.addCleanup(self.b.close)
        self.b.observe(ROBOTS); self.b.observe(PAGE)
        self.f=PublicFetch(self.b)
        self.open=Mock(); self.f.opener.open=self.open
        clock=patch('experiments.free_search.owner_public_fetch.time.time',return_value=100)
        self.clock=clock.start(); self.addCleanup(clock.stop)

    def reply(self, body, status=200):
        response=Mock(status=status,headers=Message()); response.read.return_value=body
        self.open.return_value.__enter__=Mock(return_value=response)
        self.open.return_value.__exit__=Mock(return_value=False)

    def test_robots_disallow_prevents_get(self):
        self.reply(b'User-agent: *\nDisallow: /uk/\n')
        self.f.get(ROBOTS,1000); self.clock.return_value=105
        with self.assertRaisesRegex(Denied,'robots_required'): self.f.get(PAGE,1000)
        self.assertEqual(self.open.call_count,1)

    def test_wildcard_robots_denial_spends_no_page_request(self):
        self.reply(b'User-agent: *\nDisallow: *last*\n')
        self.f.get(ROBOTS,1000); self.clock.return_value=105
        with self.assertRaisesRegex(Denied,'robots_required'): self.f.get(PAGE,1000)
        self.assertEqual(self.open.call_count,1)
        self.assertEqual(self.b.summary()['reserved_gets'],1)

    def test_allowed_html_has_no_publication_claim(self):
        self.reply(b'User-agent: *\nAllow: /\n')
        self.f.get(ROBOTS,1000); self.clock.return_value=105
        self.reply(b'<html>public</html>')
        body, evidence=self.f.get(PAGE,1000)
        self.assertEqual(body,'<html>public</html>')
        self.assertIsNone(evidence['publication_at']); self.assertEqual(evidence['paid_api_calls'],0)
        req=self.open.call_args.args[0]
        self.assertEqual(req.get_method(),'GET')
        self.assertIsNone(req.get_header('Cookie')); self.assertIsNone(req.get_header('Authorization'))
        self.assertEqual(self.open.call_args.kwargs,{'timeout':20})

    def test_dns_failure_not_retried_or_refunded(self):
        self.open.side_effect=URLError('private diagnostic never persisted')
        body,e=self.f.get(ROBOTS,1000)
        self.assertIsNone(body); self.assertEqual(e['error'],'URLError')
        self.assertEqual(self.open.call_count,1); self.assertEqual(self.b.summary()['reserved_bytes'],1000)

    def test_403_persists_hold(self):
        self.open.side_effect=HTTPError(ROBOTS,403,'Denied',{},None)
        self.f.get(ROBOTS,1000); self.clock.return_value=105
        with self.assertRaisesRegex(Denied,'persistent_source_hold'): self.f.get(ROBOTS,1000)
        self.assertEqual(self.open.call_count,1)

    def test_html_robots_rejected(self):
        self.reply(b'<html>Access page</html>')
        _,e=self.f.get(ROBOTS,1000)
        self.assertEqual(e['error'],'invalid_robots'); self.assertIsNone(self.f.robots)

    def test_robots_longer_delay_is_persisted(self):
        self.reply(b'User-agent: *\nAllow: /\nCrawl-delay: 15\n')
        self.f.get(ROBOTS,1000); self.clock.return_value=110
        with self.assertRaisesRegex(Denied,'completion_pause'): self.f.get(PAGE,1000)
        self.assertEqual(self.open.call_count,1)

    def test_cap_hit_stops_without_reading_an_extra_byte(self):
        self.reply(b'x'*1000)
        body,e=self.f.get(ROBOTS,1000)
        self.assertIsNone(body); self.assertEqual(e['error'],'body_cap_reached')
        self.assertTrue(self.b.summary()['source_hold'])

    def test_challenge_and_cap_hold(self):
        self.reply(b'<title>Just a moment</title>cf-chl-')
        _,e=self.f.get(ROBOTS,1000)
        self.assertEqual(e['error'],'source_challenge'); self.assertTrue(self.b.summary()['source_hold'])
