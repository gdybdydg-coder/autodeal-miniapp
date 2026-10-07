"""Offline adapter for JSON embedded in public search HTML. Never executes JS.

Only the primary rendered list is read: cloned templates, recommendations and
apiData are not data sources. No request, internal endpoint, key or paid cache.
"""
import json
import re
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit


class SearchStateError(ValueError):
    pass


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(); self.active = False; self.scripts = []
    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = True; self.scripts.append('')
    def handle_endtag(self, tag):
        if tag == 'script': self.active = False
    def handle_data(self, text):
        if self.active: self.scripts[-1] += text


def parse_search_state(html):
    if not isinstance(html, str) or len(html.encode()) > 2_500_000:
        raise SearchStateError('html_oversize')
    parser = _Scripts(); parser.feed(html)
    marker = 'window.__PINIA__ = '
    matches = [s for s in parser.scripts if marker in s]
    if len(matches) != 1 or matches[0].count(marker) != 1:
        raise SearchStateError('state_missing_or_ambiguous')
    raw = matches[0].split(marker, 1)[1]
    try:
        state, end = json.JSONDecoder().raw_decode(raw)
        if raw[end:].strip() != ';': raise ValueError()
        listing = state['list']['lists']['items']
        meta = state['searchPage']['searchResultData']
        pairs = parse_qsl(meta['searchString'], strict_parsing=True, max_num_fields=80)
        fields = dict(pairs)
        if len(fields) != len(pairs): raise ValueError()
        if not re.fullmatch(r'0|[1-9][0-9]{0,3}', fields['page']): raise ValueError()
        page, limit = int(fields['page']), int(fields['limit'])
        if str(page) != str(listing['page']) or not 1 <= limit <= 100: raise ValueError()
        items = listing['items']
        if not isinstance(items, list) or len(items) > 200: raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise SearchStateError('invalid_state') from exc
    cards, rejected, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'AdvertisementCardTemplate':
            continue
        try:
            c = item['component']['advertisementCard']['data']
            ident = c['id']; url = c['link']; parts = urlsplit(url)
            if (type(ident) is not int or c['type'] != 'Auto' or
                item.get('id') != f'Auto{ident}' or
                parts.scheme != 'https' or parts.netloc != 'auto.ria.com' or
                parts.query or parts.fragment or
                not re.fullmatch(r'/uk/auto_[a-z0-9_-]+_'+str(ident)+r'\.html', parts.path)):
                raise ValueError()
            if ident in seen: raise ValueError()
            seen.add(ident)
            usd = c.get('price', {}).get('USD')
            if type(usd) not in (int, float) or not 0 < usd < 1e9: raise ValueError()
            cards.append({'id': ident, 'url': url, 'price_usd': usd,
                          'published_at': None})
        except (KeyError, TypeError, ValueError, AttributeError):
            rejected.append('invalid_or_duplicate_primary_card')
    scope = sorted((k,v) for k,v in pairs if k not in ('page','limit'))
    return {'page_index': page, 'page_size': limit,
            'scope': dict(scope), 'scope_digest': sha256(json.dumps(scope).encode()).hexdigest(),
            'source_reported_count': meta.get('count'),
            'cards': cards, 'rejected': rejected,
            'whole_market_recall': None, 'publication_times_verified': False,
            'coverage_proven': False, 'paid_calls': 0}
