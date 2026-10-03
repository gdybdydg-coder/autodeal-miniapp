"""Observed OLX detail HTML adapter; offline, allowlisted facts only.
JSON-LD display conversion is not interpreted as a second asking price or FX quote.
Seller/contact/VIN/registration/full description data are never returned.
The complete visible description is reviewed in memory; only sanitized reasons
and a content fingerprint survive. Truncated description nodes cannot pass.
"""
from decimal import Decimal
import re
from urllib.parse import urlsplit
from .html_snapshot import SearchParser,clean_url,money
from .pipeline import canonical


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
    for field in conflicts:
        target={'engine':'engine_cc','mileage':'mileage_km'}.get(field,field)
        raw.pop(target,None)
    if 'customs' in conflicts:raw['customs_cleared']='conflicting'
    if 'technical_condition' in conflicts:raw.pop('condition',None)
    images=v.get('image',[])
    raw['photos']=list(dict.fromkeys(u for u in images if isinstance(u,str) and urlsplit(u).scheme=='https' and (urlsplit(u).hostname or '').endswith('.olxcdn.com'))) if isinstance(images,list) else []
    c=canonical(raw,fetched_at,fx_quote=fx_quote);c['research_only']=True
    c['field_conflicts']=sorted(set(conflicts))
    c['price_conflicts']=['same_currency_conflict'] if state=='same_currency_conflict' else []
    c['observed_sale_terms']=fields.get('sale_terms') if 'sale_terms' not in conflicts else None
    labels=[n.text() for n in nodes if n.attrs.get('data-testid')=='ad-posted-at']
    c['observed_publication_label']=labels[0] if len(labels)==1 else None
    c['price_observations']={'visible':{'amount':price,'currency':currency},'jsonld':{'amount':structured_price,'currency':structured_currency},'relationship':state,'fx_rate':None}
    return {'listing':c,'summary':{'bytes_parsed':len(data),'download_truncated':truncated or not p.html_closed,
        'price_relationship':state,'photo_urls':len(c['photos']),'field_conflicts':sorted(set(conflicts)),
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
