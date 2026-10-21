"""Conservative disclosure of captured visible text, never an accident-free claim.

This cannot verify hidden vehicle history or mechanical condition. A browser
capture must contain both the listing identity and visible description/history
sections; otherwise automatic delivery is blocked for review.
"""
import hashlib
import math
import re
from decimal import Decimal
from .owner_pilot import _page


def automated_review(capture, page, now):
    if not isinstance(capture,dict) or capture.get('kind')!='public_visible_body':
        raise ValueError('visible_review_capture_required')
    if capture.get('listing_id')!=page.listing_id or capture.get('listing_url')!=page.url:
        raise ValueError('visible_review_identity')
    at=capture.get('observed_at')
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=300:
        raise ValueError('visible_review_stale')
    text=capture.get('text')
    if not isinstance(text,str) or not 100<=len(text)<=200000:
        raise ValueError('visible_review_incomplete')
    if not re.search(r'ID авто\s+'+re.escape(page.listing_id)+r'\b',text):
        raise ValueError('visible_review_identity')
    if not re.search(r'Опис|Опис автомобіля',text) or not re.search(r'Історія авто|Перевірен[ао] історі|ДТП',text):
        raise ValueError('visible_history_section_unavailable')
    # Reject a price changed between HTML intake and the visible browser visit.
    # Only the short visible block immediately preceding the native price button
    # is accepted; recommended cars elsewhere in the body cannot validate price.
    if text.count('Середня ціна')!=1:raise ValueError('visible_asking_price_ambiguous')
    price_block=text.split('Середня ціна')[0][-200:]
    prices=re.findall(r'(?<![\d.,])([1-9][\d \u00a0\u202f]*)\s*\$',price_block)
    if len(prices)!=1:raise ValueError('visible_asking_price_ambiguous')
    amount=Decimal(re.sub(r'\s','',prices[0]))
    if amount!=Decimal(str(_page(page,now,300)['price'])):
        raise ValueError('visible_asking_price_changed')
    findings=[]
    for line in text.splitlines():
        if re.search(r'короз|ірж|ржав|пошкодж|після ДТП|участь у ДТП|бит[аеиій]|потребує ремонт|ремонт.*двигун|затоп',line,re.I):
            quote=' '.join(line.split())[:190]
            if quote and quote not in findings:findings.append(quote)
            if len(findings)>=3:break
    findings=['Текст відкритої сторінки: '+line for line in findings]
    findings.append('Автоматично прочитано видимі відомості; повна історія та технічний стан не підтверджені.')
    return {'listing_id':page.listing_id,'listing_url':page.url,
        'detail_sha256':hashlib.sha256(page.html.encode()).hexdigest(),'observed_at':at,
        'visible_history_checked':True,'review_method':'automated_visible_text_only',
        'visible_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'visible_asking_usd':str(amount),
        'findings':findings,'card_notice':('Є зауваження в описі / історії — перевір оголошення' if len(findings)>1
            else 'Історія й технічний стан не підтверджені')}
