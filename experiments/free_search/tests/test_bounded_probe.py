import json
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from experiments.free_search.bounded_probe import allowed_url, NoRedirect, run
from experiments.free_search.replay_demo import card, detail


class ProbeTests(TestCase):
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

    def test_denial_stops_without_retry(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError('secret', 429, 'secret', {}, None)
        with patch('experiments.free_search.bounded_probe.build_opener', return_value=Opener()):
            result = run()
        self.assertEqual(len(result['requests']), 1)
        self.assertEqual(result['status'], 'stopped')
        self.assertNotIn('secret', json.dumps(result))
