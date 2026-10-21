"""Public AUTO.RIA mean contract; no API, peers or inferred price band.

Live HTML currently verifies only the label, NOT a numeric layout. Therefore
the adapter remains unverified and blocks delivery. Numeric tests are synthetic
until a browser-visible numeric source is captured and matched to this listing.
"""
from decimal import Decimal
import hashlib
import math
import json
import re
from .public_detail_crosscheck import _Fields
from .visible_adapter import parse_visible_facts

NUMERIC_LAYOUT_VERIFIED = False
FIELD = 'basicInfoPriceGhost'


def browser_quote(capture, page, now):
    """Validate a minimal visible calculator capture; never infer a range/mean.

    The caller captures DOM via the public browser, not developer API/cache.
    Persisted snapshots expire and cannot by themselves authorize delivery.
    """
    if not isinstance(capture, dict):
        raise ValueError('browser_mean_capture_missing')
    if capture.get('kind') == 'public_market_range':
        return browser_range_quote(capture, page, now)
    ident = page.listing_id
    if (not isinstance(ident, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', ident)
            or not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                + ident + r'\.html', page.url)
            or capture.get('listing_id') != ident or capture.get('input_id') != ident
            or capture.get('listing_url') != page.url
            or capture.get('calculator_url') !=
                'https://auto.ria.com/uk/price/average/?currentTab=byOmniId&omniId='+ident
            or not isinstance(capture.get('identity_text'), str)
            or not re.fullmatch(r'Знайдено .+ за ID оголошення '+ident, capture['identity_text'])):
        raise ValueError('browser_mean_identity_mismatch')
    observed = capture.get('observed_at')
    if (type(now) not in (int, float) or not math.isfinite(now)
            or type(observed) not in (int, float) or not math.isfinite(observed)
            or not 0 <= now-observed <= 300
            or type(page.received_at) not in (int, float) or not math.isfinite(page.received_at)
            or not 0 <= now-page.received_at <= 300):
        raise ValueError('browser_mean_stale')
    if capture.get('period') != '3 місяці':
        raise ValueError('browser_mean_period_unverified')
    values = capture.get('mean_text')
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise ValueError('browser_mean_ambiguous')
    text = ' '.join(values[0].split())
    money = r'([1-9]\d*|[1-9]\d{0,2}(?: \d{3})+)'
    match = re.fullmatch(r'Середня ціна сформована на основі проданих '+money+r' авто '
                         + money + r' \$', text)
    if not match:
        raise ValueError('browser_mean_numeric_value_unavailable')
    return {'basis': 'auto_ria_public_calculator_mean', 'source_id': ident,
            'url': capture['calculator_url'], 'currency': 'USD',
            'average_usd': str(Decimal(match[2].replace(' ', ''))),
            'provider_sample_count': int(match[1].replace(' ', '')), 'period': capture['period'],
            'observed_at': observed, 'required_peers': 0,
            'capture_sha256': hashlib.sha256(json.dumps(capture, sort_keys=True,
                ensure_ascii=False).encode()).hexdigest()}


def browser_range_quote(capture, page, now):
    ident = page.listing_id
    if (capture.get('listing_id') != ident or capture.get('listing_url') != page.url
            or capture.get('visible_listing_id') != ident
            or not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                + re.escape(ident) + r'\.html', page.url)):
        raise ValueError('browser_range_identity_mismatch')
    for stamp in (capture.get('observed_at'), page.received_at):
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or type(now) not in (int, float) or not math.isfinite(now)
                or not 0 <= now-stamp <= 300):
            raise ValueError('browser_range_stale')
    def money(field, prefix):
        texts = capture.get(field)
        if not isinstance(texts, list) or len(texts) != 1 or not isinstance(texts[0], str):
            raise ValueError('browser_range_ambiguous')
        match = re.fullmatch(prefix+r' ([1-9]\d*|[1-9]\d{0,2}(?: \d{3})+) \$', ' '.join(texts[0].split()))
        if not match:
            raise ValueError('browser_range_numeric_unavailable')
        return Decimal(match[1].replace(' ', ''))
    lower, upper = money('lower_text', 'від'), money('upper_text', 'до')
    if lower > upper:
        raise ValueError('browser_range_inverted')
    return dict(basis='auto_ria_public_market_range', source_id=ident, url=page.url,
                currency='USD', lower_usd=str(lower), upper_usd=str(upper),
                observed_at=capture['observed_at'], required_peers=0,
                capture_sha256=hashlib.sha256(json.dumps(capture, sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest())


def quote(page, now):
    if (type(now) not in (int, float) or not math.isfinite(now)
            or type(page.received_at) not in (int, float)
            or not math.isfinite(page.received_at)
            or not 0 <= now-page.received_at <= 300
            or not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_'
                                + re.escape(page.listing_id) + r'\.html', page.url)
            or parse_visible_facts(page.html, page.listing_id).listing_id != page.listing_id):
        raise ValueError('public_mean_identity_or_freshness_invalid')
    parser = _Fields({FIELD}); parser.feed(page.html)
    values = parser.values.get(FIELD, [])
    if len(values) != 1:
        raise ValueError('public_mean_missing_or_ambiguous')
    text = ' '.join(''.join(values[0]).split())
    match = re.fullmatch(r'Середня ціна\s*:?\s*([1-9]\d*(?: \d{3})*)\s*\$', text)
    if not match:
        raise ValueError('public_mean_numeric_value_unavailable')
    if not NUMERIC_LAYOUT_VERIFIED:
        raise ValueError('public_mean_numeric_layout_unverified')
    return {'basis': 'auto_ria_public_mean', 'source_id': page.listing_id,
            'url': page.url, 'currency': 'USD',
            'average_usd': str(Decimal(match[1].replace(' ', ''))),
            'observed_at': page.received_at,
            'html_sha256': hashlib.sha256(page.html.encode()).hexdigest()}
