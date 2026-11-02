"""Owner-only asking-price comparisons using public search and catalogue cards."""
import hashlib
import json
import math
import re
import time
from decimal import Decimal
from pathlib import Path
from .owner_card import safe_url
from .owner_local_bridge import atomic_json
from backend.listing_card import listing_caption


def clean(value):
    return ' '.join((value or '').split())


def number(value):
    raw = ''.join(clean(value).split())
    return int(raw) if raw.isdecimal() else 0


def mileage(text):
    found = re.fullmatch(r'([\d\s]+)\s*тис\.?\s*км', clean(text), re.I)
    return number(found[1]) if found else 0


def unsuitable(description, badges):
    value = clean(description + ' ' + badges).casefold()
    return bool(re.search(r'після дтп|після аварії|пошкоджен[оі]|невідремонтован|не на ходу|не заводиться|під відновлення|на запчастини|потребує ремонту|нерозмитнен|перший внесок', value))


def preview_subject(card):
    p = card.get('preview') or {}
    title = re.fullmatch(r'(.+) ((?:19|20)\d\d)',clean(p.get('title')))
    specs = p.get('specs',[])
    price_text = re.fullmatch(r'([\d\s]+)\s*\$',clean(p.get('price')))
    if not title or len(specs)!=4 or not price_text:
        raise ValueError('peer_preview_missing')
    model,year=title.groups()
    parts = [clean(x) for x in clean(p.get('model_info')).split('•')]
    image_title = clean(p.get('image_title'))
    if not parts[0] or title[0] not in image_title or not p.get('description'):
        raise ValueError('peer_preview_missing')
    photo=safe_url(p.get('photo'))
    if not photo or unsuitable(p['description'],p.get('badges') or ''):
        raise ValueError('peer_condition_or_photo')
    slug = re.fullmatch(r'https://auto\.ria\.com/uk/auto_([a-z0-9-]+)_([a-z0-9_-]+)_'+re.escape(card['id'])+r'\.html',card['url'])
    if not slug:raise ValueError('peer_model_unknown')
    return dict(id=card['id'],url=card['url'],model=model,year=int(year),gen=parts[0],
        mod=parts[1] if len(parts)>1 else '',body=image_title.split(title[0])[0].strip(),
        km=mileage(specs[0]),gear=clean(specs[1]),fuel=clean(specs[2]),city=clean(specs[3]),
        price=number(price_text[1]),photo=photo,path='/'.join((slug[1],slug[2].replace('_','-'))))


def pool_for(subject, cache, read):
    key=hashlib.sha256((subject['path']+str(subject['year'])).encode()).hexdigest()
    path=cache/('peer-'+key+'.json')
    if path.exists() and time.time()-path.stat().st_mtime<21600:
        return json.loads(path.read_text('utf-8'))
    url='https://auto.ria.com/uk/car/'+subject['path']+'/year/'+str(subject['year'])+'/'
    rows=read(url+'?page=1')
    if len(rows)>=20:rows+=read(url+'?page=2')
    atomic_json(path, rows)
    return rows


def reference(subject, pool):
    possible=[]
    selfrow=next((r for r in pool if r['id']==subject['id']),{})
    for row in pool:
        specs=row.get('specs',[])
        if len(specs)!=4 or row['id']==subject['id'] or not row.get('seller'):continue
        if any(clean(row.get(k))!=clean(subject[k]) for k in ('model','gen','body')) or row['year']!=subject['year']:continue
        if row['seller']==selfrow.get('seller') or row.get('vin') and row['vin']==selfrow.get('vin'):continue
        if unsuitable(row.get('description',''),row.get('badges','')):continue
        if not subject['km'] or not mileage(specs[0]):continue
        gap=abs(mileage(specs[0])-subject['km'])
        if gap>min(50,subject['km']*.35):continue
        if clean(specs[3])!=subject['gear'] or subject['gear'] in ('','Не вказано'):continue
        fuel=clean(specs[2])
        if selfrow:
            if fuel!=clean(selfrow['specs'][2]):continue
            if selfrow.get('mod') and row.get('mod') and clean(selfrow['mod'])!=clean(row['mod']):continue
        else:
            if not re.search(r'\d[.,]\d',subject['mod']) or clean(row.get('mod'))!=subject['mod']:continue
            if clean(fuel.split(',')[0])!=clean(subject['fuel'].split(',')[0]):continue
        price=number(row['price'])
        if price:possible.append((gap,row['id'],row['seller'],row.get('vin'),price))
    chosen=[];sellers=set();vins=set();ids=set()
    for gap,ident,seller,vin,price in sorted(possible):
        if ident in ids or seller in sellers or vin and vin in vins:continue
        chosen.append([ident,price]);ids.add(ident);sellers.add(seller)
        if vin:vins.add(vin)
        if len(chosen)==10:break
    return chosen


def make_card(subject, peers, filters):
    ids={p[0] for p in peers}
    if not 5<=len(peers)<=10 or len(ids)!=len(peers) or subject['id'] in ids:raise ValueError('peer_sample_insufficient')
    prices=sorted(p for _,p in peers)
    price=subject['price']
    if not price or prices[0]<=0:raise ValueError('peer_price_missing')
    ref=prices[math.ceil(len(prices)/4)-1]
    minimum=Decimal(str(filters.get('minDiscount',15)))
    discount=(1-Decimal(price)/ref)*100
    bounds=filters.get('price',{})
    if not (bounds.get('from') or 0)<=price<=(bounds.get('to') or math.inf):raise ValueError('filter_price_changed')
    if discount<minimum:raise ValueError('below_saved_discount')
    text=listing_caption(subject['model'],'',subject['year'],subject['fuel'],subject['gear'],subject['km']*1000,
        subject['city'],price,[f'📊 Нижня чверть цін {len(peers)} аналогів: ${ref:,}'.replace(',',' '),
        f'🔥 Нижче на {discount:.1f}% (${ref-price}); поріг {minimum}%', 'За цінами оголошень · стан потребує перевірки']).replace('  ·',' ·')
    if not safe_url(subject['photo']):raise ValueError('peer_photo_missing')
    return {'text':text,'photo':subject['photo'],'url':subject['url']},{'source':'public_peer_asking_v1','peers':peers,'reference':ref,'price':price}


def deliver(db,transport,owner,ident,card,evidence):
    with db:
        claimed=db.execute('INSERT OR IGNORE INTO sent VALUES(?,?,?,?)',
            (ident,'attempting',None,json.dumps(evidence))).rowcount
    if not claimed:return 'duplicate',None
    response=transport(owner,card)
    message=response.get('result',{})
    mid=message.get('message_id')
    state='sent' if response.get('ok') is True and type(mid) is int else 'uncertain'
    with db:db.execute('UPDATE sent SET state=?,message_id=? WHERE id=?',(state,mid,ident))
    return state,mid
