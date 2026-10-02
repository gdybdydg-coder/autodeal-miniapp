"""Offline adapter for observed OLX search HTML (2026-10-02).
No network. Extracts observed fields only; output is research-only, not deliverable.
Incomplete downloads and JSON-LD subsets never become proof of a complete crawl.
"""
import argparse
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urljoin,urlsplit,urlunsplit
from .pipeline import canonical

VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

class Node:
    def __init__(self,tag,attrs):self.tag=tag;self.attrs=dict(attrs);self.children=[]
    def nodes(self):
        yield self
        for c in self.children:
            if isinstance(c,Node):yield from c.nodes()
    def text(self):
        if self.tag in ('script','style'):return ''
        return ' '.join(c.text() if isinstance(c,Node) else c for c in self.children).strip()

class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.stack=[];self.cards=[];self.ld=[];self.pages=set();self.html_closed=False;self.roots=[]
    def handle_starttag(self,tag,attrs):
        node=Node(tag,attrs)
        if tag=='a' and 'page=' in node.attrs.get('href',''):self.pages.add(node.attrs['href'])
        if self.stack:self.stack[-1].children.append(node)
        else:self.roots.append(node)
        if tag not in VOID:self.stack.append(node)
    def handle_data(self,data):
        if self.stack:self.stack[-1].children.append(data)
    def handle_endtag(self,tag):
        if tag=='html':self.html_closed=True
        found=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i].tag==tag),None)
        if found is None:return
        node=self.stack[found];self.stack=self.stack[:found]
        if node.attrs.get('data-testid')=='l-card':self.cards.append(node)
        if tag=='script' and node.attrs.get('type')=='application/ld+json':
            try:self.ld.append(json.loads(''.join(x for x in node.children if isinstance(x,str))))
            except ValueError:pass


def clean_url(url):
    parts=urlsplit(urljoin('https://www.olx.ua',url))
    if parts.scheme!='https' or parts.hostname not in ('olx.ua','www.olx.ua'):return None
    return urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))


def money(text):
    # Displayed currency only, never guess original currency or exchange rate.
    text=text.replace('\xa0',' ').replace('\u202f',' ')
    m=re.match(r'^\s*([\d ]+(?:[.,]\d{1,2})?)\s*(грн\.?|\$|€)(?:\s|$)',text)
    if not m:return None,None
    try:value=Decimal(m[1].replace(' ','').replace(',','.'))
    except InvalidOperation:return None,None
    return str(value),{'грн':'UAH','грн.':'UAH','$':'USD','€':'EUR'}[m[2]]


def parse_search_snapshot(data, *, fetched_at, truncated):
    if len(data)>2*1024*1024:raise ValueError('Input exceeds 2 MiB')
    if type(fetched_at) is not int or fetched_at<=0:raise ValueError('Explicit observation timestamp required')
    parser=SearchParser();parser.feed(data.decode('utf-8',errors='replace'));parser.close()
    listings={};rejected=0;duplicates=0
    for node in parser.cards:
        nodes=list(node.nodes())
        def first(testid):return next((n for n in nodes if n.attrs.get('data-testid')==testid),None)
        link=first('card-title-link');price=first('ad-price');place=first('location-date')
        id=node.attrs.get('id');url=clean_url(link.attrs.get('href','')) if link else None
        if not id or not id.isdecimal() or not url or '/d/' not in url:rejected+=1;continue
        value,currency=money(price.text() if price else '')
        raw=dict(id=id,source='olx',url=url,title=link.text(),price=value,currency=currency,
                 price_kind=None,category=None,publication_verified=False,checked_at=fetched_at,
                 evidence='observed_search_html',photos=[])
        images=[n.attrs.get('src') for n in nodes if n.tag=='img']
        raw['photos']=[u for u in images if isinstance(u,str) and urlsplit(u).scheme=='https' and (urlsplit(u).hostname or '').endswith('.olxcdn.com')]
        # Numeric characteristics appear in a separate span, not inferred from title.
        for n in nodes:
            if n.tag!='span':continue
            text=n.text()
            m=re.fullmatch(r'(\d{4})\s+([\d\s]+)\s+тис\.км\.',text)
            if m:raw.update(year=int(m[1]),mileage_km=int(m[2].replace(' ',''))*1000)
            m=re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*л\.',text)
            if m:raw['engine_cc']=int(Decimal(m[1].replace(',','.'))*1000)
            if text in ('Бензин','Дизель','Газ / бензин','Гібрид','Електро'):
                raw['fuel']={'Бензин':'petrol','Дизель':'diesel','Газ / бензин':'gas_petrol','Гібрид':'hybrid','Електро':'electric'}[text]
            if text in ('Механічна','Автоматична','Варіатор','Типтронік','Роботизована'):
                raw['transmission']={'Механічна':'manual','Автоматична':'automatic','Варіатор':'cvt','Типтронік':'tiptronic','Роботизована':'robotized'}[text]
        c=canonical(raw,fetched_at)
        c['observed_location_date']=place.text() if place else None
        c['research_only']=True
        if id in listings:duplicates+=1
        else:listings[id]=c
    offers=[]
    for x in parser.ld:
        if isinstance(x,dict) and x.get('@type')=='Product' and isinstance(x.get('offers'),dict):
            candidates=x['offers'].get('offers',[])
            if isinstance(candidates,list):offers.extend(c for c in candidates if isinstance(c,dict))
    by_url={c['url']:c for c in listings.values()}
    for offer in offers:
        url=clean_url(offer.get('url',''))
        photos=offer.get('image',[])
        if url in by_url and isinstance(photos,list):
            safe=[u for u in photos if isinstance(u,str) and urlsplit(u).scheme=='https' and (urlsplit(u).hostname or '').endswith('.olxcdn.com')]
            by_url[url]['photos']=list(dict.fromkeys(by_url[url]['photos']+safe))
    subset_urls={clean_url(x['url']) for x in offers if isinstance(x.get('url'),str)}-{None}
    card_urls={c['url'] for c in listings.values()}
    return dict(listings=list(listings.values()),summary=dict(
        fetched_at=fetched_at,bytes_parsed=len(data),download_truncated=truncated or not parser.html_closed,
        closed_cards=len(parser.cards),unique_cards=len(listings),duplicate_cards=duplicates,rejected_cards=rejected,
        jsonld_offer_count=len(offers),jsonld_urls_in_cards=len(subset_urls&card_urls),
        cards_absent_from_jsonld=len(card_urls-subset_urls),cards_with_photo_urls=sum(bool(c['photos']) for c in listings.values()),observed_pagination_links=sorted(parser.pages),
        publication_verified=0,whole_car_category_verified=0,full_price_verified=0,
        collection_complete=False,ready_for_delivery=False))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--fetched-at',required=True,type=int)
    ap.add_argument('--truncated',action='store_true');args=ap.parse_args()
    with Path(args.input).open('rb') as f:data=f.read(2*1024*1024+1)
    print(json.dumps(parse_search_snapshot(data,fetched_at=args.fetched_at,truncated=args.truncated)['summary'],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
