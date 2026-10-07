import json
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from experiments.free_search.bounded_probe import allowed_url, NoRedirect, run
from experiments.free_search.replay_demo import card, detail


class ProbeTests(TestCase):
    def test_successful_http_does_not_hide_inconsistent_pagination(self):
        from experiments.free_search.bounded_probe import ALLOWED_FEEDS
        from experiments.free_search.tests.test_public_pagination import fixture
        class Response:
            status = 200
            headers = {}
            def __init__(self,body): self.body=body.encode()
            def read(self,limit): return self.body[:limit]
            def __enter__(self): return self
            def __exit__(self,*args): pass
        class Opener:
            def open(self,request,timeout):
                if request.full_url.endswith('robots.txt'):
                    return Response('User-agent: *\nAllow: /')
                index=ALLOWED_FEEDS.index(request.full_url)
                return Response(fixture(index,20 if index==2 else 100)+card(str(800+index)))
        with patch('experiments.free_search.bounded_probe.build_opener',return_value=Opener()),patch('time.sleep'):
            result=run(feed_urls=ALLOWED_FEEDS,detail_limit=0)
        self.assertEqual(result['status'],'bounded_observation_complete')
        self.assertEqual(result['coverage']['state'],'incomplete')
        self.assertIn('page_size_changed',result['coverage']['reasons'])
        self.assertEqual(len(result['requests']),4)
        self.assertEqual(result['details'],[])

    def test_credentials_rejected_before_transport(self):
        with patch.dict('os.environ', {'RIA_API_KEY':'synthetic'}), patch('experiments.free_search.bounded_probe.build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'production_configuration_present'):
                run()
            opener.assert_not_called()

    def test_alternative_is_explicit_and_bounded_before_io(self):
        from experiments.free_search.bounded_probe import ALLOWED_FEEDS
        with patch('experiments.free_search.bounded_probe.build_opener') as opener:
            for feeds, limit in ((ALLOWED_FEEDS,2), (('https://evil.test',),0),
                                 ((ALLOWED_FEEDS[0],ALLOWED_FEEDS[0]),0), (ALLOWED_FEEDS,True)):
                with self.subTest(feeds=feeds,limit=limit), self.assertRaises(ValueError):
                    run(feed_urls=feeds,detail_limit=limit)
            opener.assert_not_called()
    def test_cannot_reach_paid_api_telegram_or_redirect(self):
        for url in ('https://developers.ria.com/auto/search', 'https://api.telegram.org/bot/sendMessage',
                    'https://auto.ria.com/api/test', 'https://auto.ria.com@evil.test/uk/last/hour/',
                    'http://auto.ria.com/uk/last/hour/', 'https://auto.ria.com/uk/auto_a_123.html?api_key=x'):
            self.assertFalse(allowed_url(url))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://developers.ria.com'))

    def test_five_requests_only_and_no_cookies_or_contacts(self):
        requests = []
        class Response:
            status = 200
            headers = {}
            def __init__(self, body): self.body = body.encode()
            def read(self, limit): return self.body[:limit]
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                u = request.full_url
                body = 'User-agent: *\nAllow: /' if u.endswith('robots.txt') else card('800') if u.endswith('/hour/') else card('801') if '?page=2' in u else detail('800' if '800' in u else '801')
                return Response(body+'<!-- private-phone-should-not-persist -->')
        with patch('experiments.free_search.bounded_probe.build_opener', return_value=Opener()), patch('time.sleep'):
            result = run()
        self.assertEqual(result['status'], 'bounded_observation_complete')
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(allowed_url(r.full_url) and r.get_method() == 'GET' and not r.has_header('Cookie') for r in requests))
        self.assertNotIn('private-phone', json.dumps(result, default=str))
        self.assertEqual(result['acquisition_basis'], 'fresh_public_html_no_paid_cache')
        self.assertIsNone(result['publication_latency_seconds'])
        for item in result['requests']:
            self.assertEqual(len(item['body_sha256']),64)
            self.assertGreaterEqual(item['completed_at'],item['started_at'])
            self.assertGreaterEqual(item['seconds'],0)
        for observation in result['details']:
            self.assertGreaterEqual(observation['parsed_at'],observation['body_received_at'])

    def test_denial_stops_without_retry(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError('secret', 429, 'secret', {}, None)
        with patch('experiments.free_search.bounded_probe.build_opener', return_value=Opener()):
            result = run()
        self.assertEqual(len(result['requests']), 1)
        self.assertEqual(result['status'], 'stopped')
        self.assertNotIn('secret', json.dumps(result))
