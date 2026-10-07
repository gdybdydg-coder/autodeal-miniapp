"""Offline adapter for JSON embedded in public search HTML. Never executes JS.

Only the primary rendered list is read: cloned templates, recommendations and
apiData are not data sources. No request, internal endpoint, key or paid cache.
"""
import json
import re
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit


from .embedded_state import EmbeddedStateError as SearchStateError, parse_embedded_state


def parse_search_state(html):
    state = parse_embedded_state(html)
    try:
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
