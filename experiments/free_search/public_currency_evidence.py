"""Supplemental primary-price currency check; no FX inference or model change."""
import re
from decimal import Decimal
from .public_details import parse_public_details
from .public_detail_crosscheck import _Fields

TOKENS={'USD':r'\$', 'UAH':r'грн\b', 'EUR':r'€'}


def currency_evidence(html, expected_id):
    d=parse_public_details(html,expected_id)
    parser=_Fields(frozenset({'basicInfoPrice'}));parser.feed(html)
    fields=parser.values.get('basicInfoPrice',[])
    text=' '.join(''.join(fields[0]).split()) if len(fields)==1 else ''
    if len(text)>500:text=''
    amounts={}
    for currency,token in TOKENS.items():
        matches=re.findall(r'(?<![\d.,])([1-9][0-9 ]*(?:[.,][0-9]{1,2})?)\s*'+token,text)
        amounts[currency]=Decimal(matches[0].replace(' ','').replace(',','.')) if len(matches)==1 else None
    visible=amounts.get(d.currency)
    agrees=visible is not None and visible==d.price
    return {'listing_id':expected_id,'jsonld_currency':d.currency,
            'jsonld_amount':str(d.price) if d.price is not None else None,
            'same_currency_visible_amount':str(visible) if visible is not None else None,
            'same_currency_agrees':agrees,
            'reason':'same_currency_source_agreement' if agrees else 'same_currency_missing_or_conflicting',
            'fx_inferred':False,'default_detail_parser_changed':False,
            'independent_price_verification':False,'ready_for_delivery':False}
