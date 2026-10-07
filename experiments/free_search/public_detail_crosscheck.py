"""Cross-check JSON-LD against allowlisted rendered DOM fields of the same page.

Same-page agreement is not independent seller verification. No seller/contact,
VIN, arbitrary page text or raw HTML is retained. No network or backend import.
"""
from html.parser import HTMLParser
from decimal import Decimal
import re
from .public_details import parse_public_details

FIELDS=frozenset({'basicInfoPrice','basicInfoTitle','basicInfoGenerationBase',
    'basicInfoTableMainInfo0','descCharacteristicsValue','descEngineEngine',
    'descTransmissionTransmission','descDriveTypeDriveType'})
VOID=frozenset('area base br col embed hr img input link meta param source track wbr'.split())

class _Fields(HTMLParser):
    def __init__(self):
        super().__init__();self.stack=[];self.values={}
    def handle_starttag(self,tag,attrs):
        d=dict(attrs);hidden=(tag in {'script','style','template','noscript'} or
            'hidden' in d or d.get('aria-hidden')=='true' or
            'display:none' in (d.get('style') or '').replace(' ','').lower() or
            bool(self.stack and self.stack[-1][2]))
        ident=d.get('id');parts=None
        if ident in FIELDS and not hidden:
            parts=[];self.values.setdefault(ident,[]).append(parts)
        if tag not in VOID:self.stack.append((tag,parts,hidden))
    def handle_endtag(self,tag):
        if tag in VOID:return
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break
    def handle_data(self,text):
        if self.stack and not self.stack[-1][2]:
            for _,parts,_ in self.stack:
                if parts is not None:parts.append(text)


def crosscheck_details(html,expected_id):
    details=parse_public_details(html,expected_id)
    parser=_Fields();parser.feed(html)
    issues=[];fields={}
    for key,values in parser.values.items():
        if len(values)!=1:
            issues.append('ambiguous_visible_'+key);continue
        text=' '.join(''.join(values[0]).split())
        if len(text)>500:issues.append('oversize_visible_'+key);continue
        fields[key]=text
    text=fields.get('basicInfoPrice','')
    # Ignore monthly leasing blocks and only accept an explicit dollar amount.
    matches=re.findall(r'(?<![\d.,])([1-9][0-9 ]*(?:[.,][0-9]{1,2})?)\s*\$',text)
    price=Decimal(matches[0].replace(' ','').replace(',','.')) if len(matches)==1 else None
    price_agrees=price is not None and details.currency=='USD' and price==details.price
    if not price_agrees:issues.append('visible_price_missing_or_conflicting')
    body=fields.get('descCharacteristicsValue','').split('•',1)[0].strip() or None
    # The literal known label is retained; never guess from model/title.
    engine=fields.get('descEngineEngine','')
    liters=re.findall(r'(?<!\d)(\d{1,2}(?:[.,]\d{1,2})?)\s*л\b',engine)
    cc=int(Decimal(liters[0].replace(',','.'))*1000) if len(liters)==1 else None
    trans=fields.get('descTransmissionTransmission')
    if trans is not None and details.transmission is not None and trans!=details.transmission:
        issues.append('transmission_representation_differs')
    mileage_text=fields.get('basicInfoTableMainInfo0','')
    m=re.fullmatch(r'(\d+(?:[.,]\d+)?) тис\. км',mileage_text)
    km=Decimal(m[1].replace(',','.'))*1000 if m else None
    if km is not None and details.mileage_km is not None and km!=details.mileage_km:
        issues.append('mileage_conflict')
    return {'id':expected_id,'visible_price_usd':str(price) if price is not None else None,
            'jsonld_price':str(details.price) if details.price is not None else None,
            'jsonld_currency':details.currency,'price_agrees':price_agrees,
            'body':body,'engine_cc':cc,'generation_text':fields.get('basicInfoGenerationBase'),
            'transmission_text':trans,'drive_text':fields.get('descDriveTypeDriveType'),
            'visible_mileage_km':str(km) if km is not None else None,
            'issues':issues,'independent_seller_verification':False,
            'ready_for_delivery':False}
