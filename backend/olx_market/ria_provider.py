"""Offline preparation for OLX parameter-based RIA observations.

No credentials, transport, production imports, readiness override or sender.
An observed provider aggregate is NOT a validated OLX market assessment.
Keep this separate from the listing-specific AUTO.RIA valuation policy.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

VERSION = 'olx-ria-parameters-v1'
DOCUMENTATION = ('https://docs-developers.ria.com/en/used-cars/'
                 'average_price/auto_ria_average_price_ai')
IDS = {'brand': 'brandId', 'model': 'modelId', 'generation': 'generationId',
       'body': 'bodyId', 'fuel': 'fuelId', 'transmission': 'gearBoxId',
       'drive_type': 'driveId'}
BIND_FIELDS = ('source', 'id', 'url', 'brand', 'model', 'generation', 'body',
               'fuel', 'transmission', 'engine_cc', 'power_hp', 'drive_type',
               'modification', 'year', 'mileage_km', 'research_condition',
               'price', 'currency', 'checked_at')


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('invalid_numeric_value')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid_numeric_value') from None
    if not result.is_finite() or result <= 0:
        raise ValueError('invalid_numeric_value')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def binding(car):
    return digest({k: car.get(k) for k in BIND_FIELDS})


def official_mapping(car, mapping, field, now):
    row = mapping.get(field, {})
    url = urlsplit(row.get('dictionary_url', ''))
    if (car.get(field) is None or row.get('value') != car[field]
            or type(row.get('id')) is not int or row['id'] <= 0
            or url.scheme != 'https' or url.hostname != 'developers.ria.com'
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith('/auto/')
            or type(row.get('checked_at')) is not int
            or not 0 <= now-row['checked_at'] <= 30*86400):
        raise ValueError('mapping_unverified_'+field)
    return str(row['id'])


def prepare(car, mapping, now, *, period_parameter):
    """Exact observed criteria; no OLX ID in omniId, no model defaults.

Mapping evidence must come from official dictionaries, not an OLX enum or a
nearby AUTO.RIA listing. This validates structure, not the truth of evidence.
No API call is made. Period is intentionally not labelled hours or days.
"""
    from .source_tracking import public_detail_url
    if (car.get('source') != 'olx' or not isinstance(car.get('id'), str)
            or not car['id'].isdigit() or not public_detail_url(car.get('url'))
            or type(car.get('checked_at')) is not int
            or not 0 <= now-car['checked_at'] <= 300):
        raise ValueError('target_identity_or_freshness_invalid')
    if (car.get('category') != 'whole_passenger_car'
            or car.get('eligibility_review', {}).get('status') != 'allowed'
            or car.get('eligibility_review', {}).get('description_complete') is not True
            or any(car.get(k) for k in ('field_conflicts', 'research_field_conflicts',
                                      'price_conflicts'))):
        raise ValueError('target_eligibility_pending')
    if type(period_parameter) is not int or period_parameter not in (90, 168):
        raise ValueError('period_not_reviewed')
    params = {'categoryId': '1'}
    for field, provider_field in IDS.items():
        params[provider_field] = official_mapping(car, mapping, field, now)
    if car.get('modification') is not None:
        params['modificationId'] = official_mapping(car, mapping, 'modification', now)
    year = car.get('year')
    if type(year) is not int or not 1900 <= year <= 2100:
        raise ValueError('year_missing')
    params['year'] = {'gte': str(year), 'lte': str(year)}
    for field, provider_field in (('mileage_km', 'mileage'), ('engine_cc', 'engineVolume')):
        value = format((positive(car.get(field))/1000).normalize(), 'f')
        params[provider_field] = {'gte': value, 'lte': value}
    params['power'] = float(positive(car.get('power_hp')))
    # Running does not mean undamaged. No implicit technicalConditionId=1,
    # damage=0 or paintConditionId=1. Unexpressed condition needs peer review.
    body = {'langId': 4, 'period': period_parameter, 'params': params}
    return {'version': VERSION, 'source': 'olx', 'source_id': car['id'],
            'target_binding': binding(car), 'body': body,
            'request_sha256': digest(body),
            'mapping_evidence': {k: {f: mapping[k][f] for f in
                ('value', 'id', 'dictionary_url', 'checked_at')} for k in
                (*IDS, 'modification') if k in mapping and k in car
                and car[k] is not None},
            'condition': car.get('research_condition'),
            'status': 'prepared_not_requested', 'technical_ready': False,
            'requires': ['separate_paid_budget', 'live_response',
                         'compatible_independent_peer_details', 'frozen_controls']}


def observe(data, request, car, now):
    """Sanitize a parameter-bound response; never unlock a Telegram delivery."""
    if (request.get('version') != VERSION or request.get('source') != 'olx'
            or request.get('source_id') != car.get('id')
            or request.get('target_binding') != binding(car)
            or request.get('request_sha256') != digest(request.get('body'))
            or 'omniId' in request.get('body', {}).get('params', {})
            or type(car.get('checked_at')) is not int
            or not 0 <= now-car['checked_at'] <= 300):
        raise ValueError('response_target_binding_invalid')
    blocks = data.get('statisticData') if isinstance(data, dict) else None
    blocks = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'avgPrice'] if isinstance(blocks, list) else []
    if len(blocks) != 1 or not isinstance(blocks[0].get('price'), dict):
        raise ValueError('provider_average_missing_or_ambiguous')
    block = blocks[0]
    average = positive(block['price'].get('USD'))
    radius = block.get('avgValueRange')
    bounds = None
    if radius is not None:
        radius = positive(radius)
        if radius >= 1:
            raise ValueError('provider_range_invalid')
        bounds = {'low': str(average*(1-radius)), 'high': str(average*(1+radius))}
    peers = data.get('similarCars', [])
    if not isinstance(peers, list):
        raise ValueError('provider_peers_invalid')
    ids = sorted({str(p['id']) for p in peers if isinstance(p, dict)
                  and type(p.get('id')) is int and p['id'] > 0})
    quantity = block.get('quantityAdv')
    if quantity is not None and (type(quantity) is not int or quantity < 0):
        raise ValueError('provider_quantity_invalid')
    return {'version': VERSION, 'source': 'olx', 'source_id': car['id'],
            'target_binding': binding(car), 'request_sha256': request['request_sha256'],
            'period_parameter': request['body']['period'], 'observed_at': now,
            'valuation_source': 'AUTO.RIA API', 'currency': 'USD',
            'provider_average_usd': str(average), 'provider_range_usd': bounds,
            'provider_quantity': quantity, 'returned_ad_ids': ids,
            'returned_distinct_ad_count': len(ids),
            'verified_independent_compatible_count': None,
            'status': 'provider_observation_not_admitted', 'technical_ready': False,
            'blockers': ['peer_compatibility_and_independence_unverified',
                         'target_and_known_crosspost_exclusion_unverified',
                         'condition_compatibility_unverified', 'frozen_controls_pending']
                         + ([] if bounds else ['provider_range_missing']),
            'asking_price_not_confirmed_sale': True,
            'extra_ria_margin_percent': '0'}


def discount_percent(reference_usd, asking_usd):
    """Pure arithmetic only; not a valuation or permission to send."""
    reference = positive(reference_usd)
    return (reference-positive(asking_usd))/reference*100
