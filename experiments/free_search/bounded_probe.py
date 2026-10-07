"""One bounded public-source observation, not a continuous collector.

At most five requests: robots, two already-observed public feed routes, and
two detail links actually returned by those pages. No paid API fallback.
Raw HTML, seller/contact/VIN data, cookies and descriptions are not retained.
Continuous use remains subject to clarification of AUTO.RIA service terms.
"""
from collections import Counter
from dataclasses import asdict
from html.parser import HTMLParser
import json
import hashlib
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.robotparser import RobotFileParser

from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.public_details import parse_public_details
from experiments.free_search.visible_adapter import parse_visible_facts
from experiments.free_search.network_guard import CREDENTIAL_NAMES

BASE = 'https://auto.ria.com'
FEEDS = (BASE+'/uk/last/hour/', BASE+'/uk/last/hour/?page=2')
ALLOWED_FEEDS = (*FEEDS, BASE+'/uk/last/hour/?page=3')
ROBOTS = BASE+'/robots.txt'
AGENT = 'AutoDeal-Research/0.1'
MAX_BYTES = 2500000


def allowed_url(url):
    value = urlsplit(url)
    return (value.scheme == 'https' and value.netloc == 'auto.ria.com' and not value.fragment
            and (url in (*ALLOWED_FEEDS, ROBOTS) or
                 (not value.query and re.fullmatch(r'/uk/auto_[A-Za-z0-9_-]+_[1-9][0-9]{0,11}\.html', value.path))))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LinkCensus(HTMLParser):
    """Separate markup census: extraction cross-check, NOT market recall."""
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == 'a' and 'm-link-ticket' in (data.get('class') or '').split():
            href = data.get('href') or ''
            match = re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_([1-9][0-9]{0,11})\.html', href)
            if match:
                self.ids.add(match[1])


def run(*, feed_urls=FEEDS, detail_limit=2):
    if (not isinstance(feed_urls, tuple) or not feed_urls or len(set(feed_urls)) != len(feed_urls)
            or any(url not in ALLOWED_FEEDS for url in feed_urls)
            or type(detail_limit) is not int or not 0 <= detail_limit <= 2
            or 1 + len(feed_urls) + detail_limit > 5):
        raise ValueError('route_or_budget_denied')
    if any(os.environ.get(name) for name in CREDENTIAL_NAMES):
        raise ValueError('production_configuration_present')
    opener = build_opener(NoRedirect())
    report = {'started_at': time.time(), 'requests': [], 'feeds': [], 'details': [],
              'paid_api_requests': 0, 'telegram_requests': 0,
              'whole_market_recall': None, 'free_valuation_proven': False,
              'environment': 'local_research_not_render',
              'acquisition_basis': 'fresh_public_html_no_paid_cache',
              'publication_latency_seconds': None, 'telegram_delivery_latency_seconds': None}
    robots = RobotFileParser()

    def fetch(url):
        if not allowed_url(url) or len(report['requests']) >= 5:
            raise ValueError('route_or_budget_denied')
        if url != ROBOTS and not robots.can_fetch(AGENT, url):
            raise ValueError('robots_denied')
        if report['requests']:
            # A longer declared source delay stops this small diagnostic.
            delay = robots.crawl_delay(AGENT) or 3
            if delay > 10:
                raise ValueError('source_delay_requires_separate_observation')
            time.sleep(delay)
        item = {'url': url, 'started_at': time.time()}
        started_mono = time.monotonic()
        report['requests'].append(item)  # Charge before I/O, including failures.
        try:
            with opener.open(Request(url, headers={'User-Agent': AGENT, 'Accept': 'text/html,text/plain'}), timeout=20) as response:
                item['status'] = response.status
                raw = response.read(MAX_BYTES+1)
                item['bytes'] = len(raw)
                item['http_date'] = response.headers.get('Date')
                item['cache_age'] = response.headers.get('Age')
                item['cache_control'] = response.headers.get('Cache-Control')
                item['etag'] = response.headers.get('ETag')
                if len(raw) > MAX_BYTES:
                    raise ValueError('body_oversize')
                item['body_sha256'] = hashlib.sha256(raw).hexdigest()
                return raw.decode('utf-8')
        except HTTPError as exc:
            item['status'] = exc.code
            raise ValueError('http_denied_or_unavailable') from None
        finally:
            item['completed_at'] = time.time()
            item['seconds'] = round(time.monotonic()-started_mono, 6)

    try:
        robots.parse(fetch(ROBOTS).splitlines())
        cards = {}
        for url in feed_urls:
            html = fetch(url)
            observed = time.time()
            census = LinkCensus(); census.feed(html)
            rows = parse_public_cards(html)
            parsed_at = time.time()
            parsed_ids = {c.listing_id for c in rows}
            report['feeds'].append({'url': url, 'observed_at': observed, 'cards': len(rows),
                'issues': dict(Counter(issue for c in rows for issue in c.issues)),
                'independent_link_census': len(census.ids),
                'link_ids_missed_by_parser': sorted(census.ids-parsed_ids),
                'parsed_at': parsed_at,
                'rows': [asdict(c) for c in rows]})
            for c in rows:
                if c.url and not c.issues:
                    cards.setdefault(c.listing_id, c)
        for c in list(cards.values())[:detail_limit]:
            html = fetch(c.url)
            item = {'listing_id': c.listing_id, 'body_received_at': time.time()}
            for name, parser in [('details', parse_public_details), ('visible', parse_visible_facts)]:
                try:
                    item[name] = asdict(parser(html, c.listing_id))
                except ValueError as exc:
                    item[name+'_error'] = str(exc)
            report['details'].append(item)
            item['parsed_at'] = time.time()
        report['status'] = 'bounded_observation_complete'
    except Exception as exc:
        # Stable classifications only, never request/credential-bearing errors.
        report['status'] = 'stopped'
        report['reason'] = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z_]+', str(exc)) else type(exc).__name__
    report['finished_at'] = time.time()
    return report


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
