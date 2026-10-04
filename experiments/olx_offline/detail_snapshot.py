"""Observed OLX detail HTML adapter; offline, allowlisted facts only.
JSON-LD display conversion is not interpreted as a second asking price or FX quote.
Seller/contact/VIN/registration/full description data are never returned.
The complete visible description is reviewed in memory; only sanitized reasons
and a content fingerprint survive. Truncated description nodes cannot pass.
"""
from decimal import Decimal
import json
import re
from urllib.parse import parse_qs, urlsplit
from .html_snapshot import SearchParser,clean_url,money
from .pipeline import canonical
from .source_dates import ASSIGNMENT, observe_source_dates


def _asking_display_observation(nodes, raw, *, complete, price_relationship, price_review):
    """Corroborate a displayed asking amount, never its original seller currency.

    The public state observed on real pages exposes a displayValue alongside a
    regularPrice in another currency. Neither documents the original currency.
    This evidence therefore cannot turn price_kind into ``full`` or authorize
    a valuation or publication. No JavaScript or source-provided code runs.
    """
    result = {
        'status': 'needs_review', 'reasons': [],
        'amount': raw.get('price'), 'currency': raw.get('currency'),
        'basis': 'complete_visible_detail_and_matching_public_state',
        'original_seller_currency_verified': False,
        'full_price_verified': False,
        'description_reviewed_in_full': bool(complete and raw.get('description_available')),
        'limits': ['original_seller_currency_unverified', 'first_publication_unverified',
                   'asking_amount_is_not_a_completed_sale_price'],
        'state_regular_price': None,
    }
    reasons = result['reasons']
    if not complete:
        reasons.append('detail_download_incomplete')
    if not raw.get('description_available'):
        reasons.append('description_incomplete')
    if raw.get('category') != 'whole_passenger_car':
        reasons.append('whole_car_category_unconfirmed')
    if price_relationship == 'same_currency_conflict':
        reasons.append('same_currency_price_conflict')
    reasons.extend(x for x in price_review.get('reasons', []) if x != 'full_price_unconfirmed')
    scripts = [n for n in nodes if n.tag == 'script' and n.closed
               and n.attrs.get('id') == 'olx-init-config']
    if len(scripts) != 1:
        reasons.append('matching_public_state_unavailable')
        return result
    script = ''.join(x for x in scripts[0].children if isinstance(x, str))
    assignments = list(ASSIGNMENT.finditer(script))
    if len(assignments) != 1:
        reasons.append('ambiguous_or_missing_state_assignment')
        return result
    try:
        state, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(script[assignments[0].end():])
        if isinstance(state, str):
            state = json.loads(state, parse_float=Decimal)
        ad = state.get('ad', {}).get('ad', {})
    except (ValueError, AttributeError, TypeError, RecursionError):
        reasons.append('invalid_public_state_json')
        return result
    if (not isinstance(ad, dict) or type(ad.get('id')) not in (str, int)
            or str(ad['id']) != raw['id'] or not isinstance(ad.get('url'), str)
            or clean_url(ad['url']) != raw['url']):
        reasons.append('public_state_identity_mismatch')
        return result
    if ad.get('status') != 'active' or ad.get('isActive') is not True:
        reasons.append('source_ad_not_active')
    params = ad.get('params')
    sale_terms = [x for x in params if isinstance(x, dict) and x.get('key') == 'sale_terms'] if isinstance(params, list) else []
    if (len(sale_terms) != 1 or sale_terms[0].get('normalizedValue') != ['regular_sale']
            or sale_terms[0].get('value') != 'Звичайний продаж'):
        reasons.append('ordinary_sale_not_corroborated')
    visible_sale = [n.text().partition(':')[2].strip() for n in nodes if n.tag == 'p'
                    and n.text().partition(':')[0] == 'Умови продажу']
    if not visible_sale or any(x != 'Звичайний продаж' for x in visible_sale):
        reasons.append('visible_sale_terms_missing_or_conflicting')
    source_price = ad.get('price')
    if not isinstance(source_price, dict):
        reasons.append('source_price_unavailable')
        return result
    if any(source_price.get(flag) is not False for flag in ('free', 'exchange', 'budget')):
        reasons.append('nonordinary_or_unavailable_price_flags')
    state_amount, state_currency = money(source_price.get('displayValue', '')) if isinstance(source_price.get('displayValue'), str) else (None, None)
    try:
        visible_amount = Decimal(raw['price'])
        display_amount = Decimal(state_amount)
        if (not visible_amount.is_finite() or visible_amount <= 0
                or not display_amount.is_finite() or display_amount <= 0
                or visible_amount != display_amount or raw['currency'] != state_currency):
            reasons.append('visible_and_state_display_disagree')
    except (ValueError, TypeError, ArithmeticError):
        reasons.append('numeric_display_price_unavailable')
    regular = source_price.get('regularPrice')
    try:
        regular_amount = Decimal(str(regular.get('value')))
        regular_currency = regular.get('currencyCode')
        if not regular_amount.is_finite() or regular_amount <= 0 or regular_currency not in ('UAH', 'USD', 'EUR'):
            raise ValueError('Invalid reported regular price')
        result['state_regular_price'] = {'amount': str(regular_amount), 'currency': regular_currency}
        if regular_currency == raw['currency'] and regular_amount != Decimal(raw['price']):
            reasons.append('same_currency_state_price_conflict')
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        reasons.append('reported_regular_price_unavailable')
    result['reasons'] = sorted(set(reasons))
    if not result['reasons']:
        result['status'] = 'corroborated_display'
    return result


def _geography(nodes, vehicle):
    """Read explicit geography from the observed, complete breadcrumb scope.

    A region/city/district slug is never translated into another platform's ID.
    JSON-LD AdministrativeArea can mean a district, so it cannot supply a city.
    City is only corroborating evidence here, not a missing-breadcrumb fallback.
    """
    values = {key: [] for key in ('region', 'locality', 'district')}
    scopes = [n for n in nodes if n.attrs.get('data-testid') == 'breadcrumbs']
    status = 'unavailable'
    brand = vehicle.get('brand')
    category = clean_url(vehicle.get('category', '')) if isinstance(vehicle.get('category'), str) else None

    def path(url):
        value = urlsplit(url).path
        return value[3:] if value.startswith('/uk/') else value

    prefix = path(category).rstrip('/') + '/' if category else None
    if len(scopes) == 1 and not scopes[0].closed:
        status = 'incomplete'
    elif len(scopes) > 1:
        status = 'ambiguous_scope'
    elif len(scopes) == 1 and isinstance(brand, str) and prefix:
        status = 'observed'
        for node in scopes[0].nodes():
            if node.tag != 'a' or not node.closed:
                continue
            href = node.attrs.get('href', '')
            url = clean_url(href)
            label = node.text()
            expected = brand + ' - '
            if not url or not label.startswith(expected):
                continue
            location = label[len(expected):].strip()
            tail = path(url)[len(prefix):] if path(url).startswith(prefix) else ''
            if not location or len(location) > 100 or not tail.strip('/') or '/' in tail.strip('/'):
                continue
            query = parse_qs(urlsplit(href).query, keep_blank_values=True)
            if query:
                district = query.get('search[district_id]')
                if set(query) != {'search[district_id]'} or len(district) != 1 or not district[0].isdecimal():
                    continue
                key = 'district'
            else:
                key = 'region' if location.casefold().endswith(' область') else 'locality'
            observation = {'value': location, 'source': 'visible_breadcrumbs', 'url': url}
            if observation not in values[key]:
                values[key].append(observation)

    result = {}
    conflicts = []
    provenance = {}
    for key, observations in values.items():
        distinct = {item['value'].casefold() for item in observations}
        result[key] = observations[0]['value'] if len(distinct) == 1 else None
        if len(distinct) > 1:
            conflicts.append(key)
        provenance[key] = {'source': 'visible_breadcrumbs',
                           'status': 'conflicting' if len(distinct) > 1 else 'observed' if distinct else status if status != 'observed' else 'unavailable',
                           'observations': observations}

    offer = vehicle.get('offers')
    area = offer.get('areaServed') if isinstance(offer, dict) else None
    area_observation = None
    if isinstance(area, dict) and area.get('@type') in ('City', 'AdministrativeArea'):
        name = area.get('name')
        if isinstance(name, str) and 0 < len(name.strip()) <= 100:
            area_observation = {'type': area['@type'], 'name': name.strip(), 'source': 'jsonld_offers_areaServed', 'corroborates': []}
            if area['@type'] == 'City' and result['locality'] is not None:
                if name.strip().casefold() != result['locality'].casefold():
                    conflicts.append('locality')
                    result['locality'] = None
                    provenance['locality']['status'] = 'conflicting'
                    provenance['locality']['observations'].append({'value': name.strip(), 'source': 'jsonld_offers_areaServed', 'type': 'City'})
                else:
                    area_observation['corroborates'].append('locality')
            elif area['@type'] == 'AdministrativeArea':
                area_observation['corroborates'] = [key for key in ('region', 'district') if result[key] is not None and name.strip().casefold() == result[key].casefold()]
    return result, provenance, area_observation, conflicts


def parse_detail_snapshot(data, *, fetched_at, truncated, fx_quote=None):
    if len(data)>2*1024*1024:raise ValueError('Input exceeds 2 MiB')
    if type(fetched_at) is not int or fetched_at<=0:raise ValueError('Explicit observation timestamp required')
    p=SearchParser();p.feed(data.decode('utf-8',errors='replace'));p.close()
    vehicles=[x for x in p.ld if isinstance(x,dict) and x.get('@type')=='Vehicle']
    if len(vehicles)!=1:raise ValueError('Exactly one observed Vehicle object required')
    v=vehicles[0];id=v.get('sku');url=clean_url(v.get('url',''))
    if not isinstance(id,str) or not id.isdecimal() or not url or '/d/' not in url:
        raise ValueError('Unambiguous numeric listing ID and OLX URL required')
    nodes=[n for root in p.roots for n in root.nodes()]
    prices=[n for n in nodes if n.attrs.get('data-testid')=='ad-price-container']
    if len(prices)!=1:raise ValueError('Exactly one visible price container required')
    price,currency=money(prices[0].text())
    offer=v.get('offers',{});offer=offer if isinstance(offer,dict) else {}
    structured_price=offer.get('price');structured_currency=offer.get('priceCurrency')
    try:
        candidate=Decimal(str(structured_price))
        structured_price=str(candidate) if candidate.is_finite() and candidate>0 else None
    except Exception:structured_price=None
    if structured_currency not in ('UAH','USD','EUR'):structured_currency=None
    state='unavailable'
    if price is not None and structured_price is not None and currency and structured_currency:
        state='different_display_currencies' if currency!=structured_currency else ('agree' if Decimal(price)==Decimal(structured_price) else 'same_currency_conflict')
    # Visible amount retains its own currency. No conversion or silent fallback.
    raw=dict(id=id,source='olx',url=url,title=v.get('name'),brand=v.get('brand'),model=v.get('model'),
             price=price,currency=currency,price_kind=None,category=None,publication_verified=False,
             checked_at=fetched_at,evidence='observed_detail_html')
    category=v.get('category')
    if isinstance(category,str) and '/transport/legkovye-avtomobili' in category:
        raw['category']='whole_passenger_car'
    descriptions=[n for n in nodes if n.attrs.get('data-testid')=='ad_description']
    raw['description_available']=len(descriptions)==1 and descriptions[0].closed
    if raw['description_available']:
        raw['description']=descriptions[0].text()
    year=v.get('productionDate')
    if isinstance(year,str) and re.fullmatch(r'\d{4}',year):raw['year']=int(year)
    allowed={"Пробіг":'mileage',"Об'єм двигуна":'engine','Тип кузова':'body','Коробка передач':'transmission','Вид палива':'fuel','Умови продажу':'sale_terms','Розмитнена':'customs','Технічний стан':'technical_condition'}
    fields={};conflicts=[]
    for n in nodes:
        if n.tag!='p':continue
        key,sep,value=n.text().partition(':')
        if sep and key in allowed:
            label=allowed[key];value=value.strip()
            if label in fields and fields[label]!=value:conflicts.append(label)
            fields[label]=value
    m=re.fullmatch(r'([\d\s]+)\s+тис\.км\.',fields.get('mileage',''))
    if m:raw['mileage_km']=int(re.sub(r'\s','',m[1]))*1000
    m=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*л\.',fields.get('engine',''))
    if m:raw['engine_cc']=int(Decimal(m[1].replace(',','.'))*1000)
    raw['body']={'Хетчбек':'hatchback','Седан':'sedan','Універсал':'wagon','Унiверсал':'wagon'}.get(fields.get('body'))
    raw['fuel']={'Бензин':'petrol','Дизель':'diesel','Газ / бензин':'gas_petrol','Гібрид':'hybrid','Електро':'electric'}.get(fields.get('fuel'))
    raw['transmission']={'Механічна':'manual','Автоматична':'automatic','Варіатор':'cvt','Типтронік':'tiptronic','Роботизована':'robotized'}.get(fields.get('transmission'))
    if fields.get('customs') in ('Так','Ні'):
        raw['customs_cleared']=fields['customs']=='Так'
    # Exact observed source wording, not a claim of independently verified state.
    if fields.get('technical_condition')=='На ходу, технічно справна':
        raw['condition']='normal'
    geography, geography_provenance, area_observation, geography_conflicts = _geography(nodes, v)
    raw.update(geography)
    conflicts.extend(geography_conflicts)
    for field in conflicts:
        target={'engine':'engine_cc','mileage':'mileage_km'}.get(field,field)
        raw.pop(target,None)
    if 'customs' in conflicts:raw['customs_cleared']='conflicting'
    if 'technical_condition' in conflicts:raw.pop('condition',None)
    images=v.get('image',[])
    raw['photos']=list(dict.fromkeys(u for u in images if isinstance(u,str) and urlsplit(u).scheme=='https' and (urlsplit(u).hostname or '').endswith('.olxcdn.com'))) if isinstance(images,list) else []
    c=canonical(raw,fetched_at,fx_quote=fx_quote);c['research_only']=True
    c['observed_asking_display'] = _asking_display_observation(
        nodes, raw, complete=not truncated and p.html_closed,
        price_relationship=state, price_review=c['price_review'])
    c['district']=geography['district']
    c['field_provenance']=geography_provenance
    c['observed_area_served']=area_observation
    c['source_date_observations']=observe_source_dates(nodes,expected_id=id,fetched_at=fetched_at)
    c['field_conflicts']=sorted(set(conflicts))
    c['price_conflicts']=['same_currency_conflict'] if state=='same_currency_conflict' else []
    c['observed_sale_terms']=fields.get('sale_terms') if 'sale_terms' not in conflicts else None
    labels=[n.text() for n in nodes if n.attrs.get('data-testid')=='ad-posted-at']
    c['observed_publication_label']=labels[0] if len(labels)==1 else None
    c['price_observations']={'visible':{'amount':price,'currency':currency},'jsonld':{'amount':structured_price,'currency':structured_currency},'relationship':state,'fx_rate':None}
    return {'listing':c,'summary':{'bytes_parsed':len(data),'download_truncated':truncated or not p.html_closed,
        'price_relationship':state,'photo_urls':len(c['photos']),'field_conflicts':sorted(set(conflicts)),
        'source_date_identity_matches':c['source_date_observations']['identity_matches'],
        'source_date_fields':sorted(c['source_date_observations']['values']),
        'full_price_verified':False,'publication_verified':False,'ready_for_delivery':False,'seller_data_exported':False}}


def main():
    import argparse
    import json
    from pathlib import Path
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--fetched-at',required=True,type=int)
    ap.add_argument('--truncated',action='store_true');args=ap.parse_args()
    with Path(args.input).open('rb') as f:data=f.read(2*1024*1024+1)
    result=parse_detail_snapshot(data,fetched_at=args.fetched_at,truncated=args.truncated)
    print(json.dumps(result['summary'],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
