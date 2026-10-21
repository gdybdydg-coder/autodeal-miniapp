"""Compact owner card. Public proof stays public; never forge paid API evidence."""
from decimal import Decimal
from html.parser import HTMLParser
import re
from .owner_pilot import _page
from .visible_adapter import parse_visible_facts
from .owner_price_rule import evaluate
from .owner_risk_review import review_text


def safe_url(value):
    # Pure validation only: never imports/downloads photos through the paid worker.
    if isinstance(value,str) and len(value)<=1000 and re.fullmatch(
            r'https://cdn\d*\.riastatic\.com/photosnew/auto/photo/[a-zA-Z0-9_-]+\.(?:jpe?g|png|webp)',value):
        return value
    return None


class _Photo(HTMLParser):
    def __init__(self):
        super().__init__(); self.urls=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='meta' and attrs.get('property')=='og:image':
            value=attrs.get('content','')
            if safe_url(value): self.urls.append(safe_url(value))


def card(page, valuation, review, now):
    row=_page(page,now,300)
    review_text(review,page,now)  # Same mandatory evidence binding and freshness.
    if (valuation.get('basis')!='auto_ria_public_market_range'
            or valuation.get('source_id')!=page.listing_id or valuation.get('currency')!='USD'):
        raise ValueError('card_public_range_required')
    rule=evaluate(valuation.get('lower_usd'),row['price'],valuation.get('required_discount_percent'))
    if not rule['eligible'] or any(valuation.get(k)!=v for k,v in rule.items()):
        raise ValueError('card_price_rule_mismatch')
    visible=parse_visible_facts(page.html,page.listing_id)
    details=[]
    if row.get('fuel'): details.append('⛽️ '+row['fuel'])
    if row.get('transmission'): details.append('⚙️ '+row['transmission'])
    if row.get('mileage_km') is not None:
        mileage=f"{float(row['mileage_km'])/1000:g}".replace('.',',')
        details.append(f'🛣️ Пробіг: {mileage} тис. км')
    if visible.region: details.append('📍 '+visible.region)
    price=f"${Decimal(str(row['price'])):,.0f}".replace(',',' ')
    market=f"${Decimal(rule['reference_usd']):,.2f}".replace(',',' ').replace('.',',')
    benefit=f"{Decimal(rule['discount_percent']):.1f}".rstrip('0').rstrip('.').replace('.',',')
    pricing=[f'💰 Ціна: {price}',f'📊 Ринкова ціна: ≈ {market}',f'🔥 Вигода: {benefit}%']
    notice=review.get('card_notice')
    if notice is not None and (not isinstance(notice,str) or not notice.strip() or len(notice)>180):
        raise ValueError('invalid_card_notice')
    if review['findings']:
        pricing.append('⚠️ '+(notice.strip() if notice else 'Є зауваження до стану / історії — перевір опис'))
    sections=['🧪 Тест',f"🚘 {row['brand']} {row['model']} · {row['year']}"]
    if details: sections.append('\n'.join(details))
    sections.extend(('\n'.join(pricing),'/stop — вимкнути сповіщення'))
    text='\n\n'.join(sections)
    if len(text.encode('utf-16-le'))//2>1000: raise ValueError('card_caption_too_long')
    parser=_Photo(); parser.feed(page.html)
    photos=set(parser.urls)
    return {'text':text,'photo':next(iter(photos)) if len(photos)==1 else None,
            'reply_markup':{'inline_keyboard':[[{'text':'🔗 Відкрити оголошення','url':page.url}]]}}
