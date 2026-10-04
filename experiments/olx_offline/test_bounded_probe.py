import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from email.message import Message
from experiments.olx_offline.bounded_probe import validate_url,fetch_once
from experiments.olx_offline.run_guard import RunGuard, GuardStopped, HARD_STOP
from experiments.olx_offline.test_pipeline import NOW


class BoundedProbeTests(unittest.TestCase):
    def test_explicit_larger_sample_charged_without_hidden_redirect_or_retry(self):
        url='https://www.olx.ua/uk/transport/legkovye-avtomobili/'
        calls=[];cap=3*1024*1024
        class Response:
            status=200
            headers=Message()
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,limit):calls.append(limit);return b'x'*limit
        with tempfile.TemporaryDirectory() as tmp:
            with RunGuard(Path(tmp),clock=lambda:NOW,monotonic=lambda:0) as guard:
                report,body=fetch_once(url,guard,open_url=lambda *a,**k:Response(),max_html_bytes=cap)
                self.assertEqual(calls,[cap]);self.assertEqual(len(body),cap)
                self.assertTrue(report['at_byte_cap']);self.assertEqual(report['attempts'],1)
                self.assertEqual(guard.counters['olx']['reserved_bytes'],cap)
    def test_html_cap_cannot_be_unbounded_or_boolean(self):
        for cap in (0,True,4*1024*1024+1):
            with self.assertRaises(ValueError):fetch_once('https://www.olx.ua/uk/transport/legkovye-avtomobili/',None,max_html_bytes=cap)
    def test_observed_newest_ui_urls_allowed_only_with_exact_contract(self):
        prefix='https://www.olx.ua/uk/transport/legkovye-avtomobili/'
        self.assertEqual(validate_url(prefix+'?currency=UAH&search%5Border%5D=created_at:desc',NOW),2*1024*1024)
        self.assertEqual(validate_url(prefix+'?currency=UAH&page=2&search%5Border%5D=created_at%3Adesc',NOW),2*1024*1024)
        for query in ('currency=USD','search%5Border%5D=price:asc','page=0',
                      'page=2&page=3','currency=UAH&currency=UAH',
                      'search%5Border%5D=created_at:desc&search%5Border%5D=',
                      'search%5Border%5D=created_at:desc&unknown=1'):
            with self.assertRaises(ValueError):validate_url(prefix+'?'+query,NOW)

    def test_expired_night_stays_closed_but_explicit_new_run_is_bounded(self):
        now=HARD_STOP+3600
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GuardStopped):
                with RunGuard(Path(tmp),clock=lambda:now):pass
            with RunGuard(Path(tmp),clock=lambda:now,hard_stop=now+120) as guard:
                self.assertLessEqual(guard.remaining_seconds(),120)
                self.assertEqual(guard.status()['deadline_utc'],'2026-10-03T06:02:00+00:00')

    def test_private_endpoints_and_unverified_query_rejected(self):
        for url in ('https://www.olx.ua/api/partner/adverts','https://api.auto.ria.com/auto/search',
                    'https://www.olx.ua/transport/legkovye-avtomobili/?sort=guessed',
                    'https://user:password@www.olx.ua/transport/legkovye-avtomobili/'):
            with self.assertRaises(ValueError):validate_url(url,NOW)

    def test_redirect_recorded_not_followed(self):
        url='https://www.olx.ua/transport/legkovye-avtomobili/?page=2'
        redirect='https://www.olx.ua/uk/transport/legkovye-avtomobili/?page=2'
        headers=Message();headers['Location']=redirect;calls=[]
        def opener(*args,**kwargs):
            calls.append(1);raise HTTPError(url,302,'Redirect',headers,None)
        with tempfile.TemporaryDirectory() as tmp:
            with RunGuard(Path(tmp),clock=lambda:NOW,monotonic=lambda:0) as guard:
                report,body=fetch_once(url,guard,open_url=opener)
                self.assertEqual(report['observed_redirect'],redirect)
                self.assertEqual(report['status'],302);self.assertEqual(body,b'')
                self.assertEqual(len(calls),1);self.assertEqual(guard.counters['olx']['requests'],1)
