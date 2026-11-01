from dataclasses import dataclass
from decimal import Decimal
import math, re
from .filter_gate import SearchFilter, Span
from .public_peer_pilot import public_peer_record
from .public_detail_crosscheck import crosscheck_details
from .visible_adapter import parse_visible_facts
@dataclass(frozen=True)
class PublicPage:
    listing_id: str
    url: str
    html: str
    received_at: float

def _page(page, now, max_age):
    if not isinstance(page, PublicPage) or type(page.received_at) not in (int, float) or (not math.isfinite(page.received_at)) or (not 0 <= now - page.received_at <= max_age) or (not isinstance(page.listing_id, str)) or (not re.fullmatch('[1-9][0-9]{0,11}', page.listing_id)) or (not re.fullmatch('https://auto\\.ria\\.com/uk/auto_[A-Za-z0-9_-]+_' + page.listing_id + '\\.html', page.url)):
        raise ValueError('public_page_identity_or_freshness_invalid')
    row = public_peer_record(page.html, page.listing_id, page.received_at)
    if parse_visible_facts(page.html, page.listing_id).listing_id != page.listing_id:
        raise ValueError('visible_listing_identity_unverified')
    cross = crosscheck_details(page.html, page.listing_id)
    if cross['issues']:
        raise ValueError('public_detail_conflict')
    row['drive'] = cross.get('drive_text')
    return row

def _filters(values):

    def bound(name, edge, multiplier=1):
        value = values.get(name, {}).get(edge)
        return None if value is None else Decimal(str(value)) * multiplier
    regions = values.get('region') or []
    if isinstance(regions, str):
        regions = [regions]
    return SearchFilter(regions=frozenset(regions), price_usd=Span(bound('price', 'from'), bound('price', 'to')), year_min=bound('year', 'from'), year_max=bound('year', 'to'), mileage_min_km=bound('mileage', 'from', 1000), mileage_max_km=bound('mileage', 'to', 1000), bodies=frozenset(values.get('body', [])), fuels=frozenset(values.get('fuel', [])), transmissions=frozenset(values.get('transmission', [])), brand=values.get('brand') or None, model=values.get('model') or None, min_discount_percent=Decimal(str(values.get('minDiscount', 15))))