"""Bounded public HTML transport for the isolated owner experiment.

No source API, authentication, cookies, proxy, redirects, retry or paid cache.
Use a fresh bounded Budget supplied by caller. Not imported by production.
"""
import time
import http.client
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .owner_budget import Denied, UA
from .owner_robots import PublicRobots

ROBOTS = 'https://auto.ria.com/robots.txt'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PublicFetch:
    def __init__(self, budget):
        self.budget = budget
        self.robots = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get_asset(self,url,cap=2500000):
        if not self.budget.allow_source_assets or not urlsplit(url).path.startswith('/vue3-prod/static/'):
            raise Denied('source_asset_not_allowed')
        return self._get(url,cap,asset=True)

    def get(self, url, cap=2500000):
        return self._get(url,cap,asset=False)

    def _get(self, url, cap, *, asset):
        allowed = self.robots is not None and self.robots.can_fetch(UA, url)
        ident = self.budget.reserve(url, cap, time.time(), robots_allowed=allowed)
        started = time.time()
        monotonic_started = time.monotonic()
        body, status, error, challenge, valid = b'', None, None, False, False
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Accept': 'application/javascript,text/javascript' if asset else 'text/html,text/plain', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=20) as response:
                status = response.status
                body = response.read(cap)
                if len(body) >= cap:
                    error, challenge = 'body_cap_reached', True
                elif response.headers.get('Content-Encoding', 'identity') != 'identity':
                    error = 'encoded_response_rejected'
                elif status != 200:
                    error = 'non_200_response'
                else:
                    html = body.decode('utf-8', errors='strict')
                    javascript = response.headers.get_content_type() in ('application/javascript','text/javascript')
                    if asset and not javascript:
                        error = 'source_asset_not_javascript'
                    # Only challenge-page markers, not generic seller mentions.
                    challenge = not (asset and javascript) and any(x in html.lower() for x in (
                        'cf-chl-', 'g-recaptcha', 'hcaptcha.com/1/api',
                        '<title>just a moment', '<title>access denied'))
                    if challenge:
                        error = 'source_challenge'
                    elif url == ROBOTS:
                        # Do not interpret an HTML block or empty file as allow-all.
                        lines = html.splitlines()
                        if '<html' in html.lower() or not any(
                                line.lower().strip().startswith('user-agent:') for line in lines):
                            error = 'invalid_robots'
                        else:
                            self.robots = PublicRobots(lines); valid = True
        except http.client.IncompleteRead as exc:
            body = exc.partial[:cap]
            error = 'incomplete_response'
        except http.client.HTTPException:
            error = 'http_protocol_error'
        except urllib.error.HTTPError as exc:
            status, error = exc.code, 'http_error'
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as exc:
            error = type(exc).__name__
        finally:
            completed = time.time()
            pause = 4
            if self.robots is not None:
                pause = max(pause, self.robots.crawl_delay(UA) or 0)
                rate = self.robots.request_rate(UA)
                if rate:
                    # Conservative one request per full rate window.
                    pause = max(pause, rate.seconds)
            self.budget.complete(ident, completed, status, len(body),
                                 challenge=challenge, robots_valid=valid, pause=pause)
        # Never expose raw transport error strings, headers, tokens or HTML in logs.
        evidence = dict(reservation=ident, url=url, status=status, error=error,
                        started_at=started, completed_at=completed, bytes=len(body),
                        elapsed_monotonic_seconds=time.monotonic()-monotonic_started,
                        publication_at=None, source_available_at=None,
                        paid_api_calls=0)
        return (None if error else body.decode('utf-8')), evidence
