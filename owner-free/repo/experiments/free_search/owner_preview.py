"""Conservative catalogue prefilter; unknown fields require detail, not guesses.

Only listing ID and icon-labelled fuel/transmission/mileage are retained.
Seller prose, identifiers, prices and arbitrary text are not used for selection.
"""
from decimal import Decimal
from html.parser import HTMLParser
import re


class PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = {}
        self.card = None
        self.parts = None
        self.kind = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        classes = set(data.get('class', '').split())
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in
                  {'script', 'style', 'template', 'noscript'} or 'hidden' in data or
                  data.get('aria-hidden') == 'true' or
                  'display:none' in data.get('style', '').replace(' ', '').lower())
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, hidden))
        if tag == 'section' and 'ticket-item' in classes and not hidden:
            ident = data.get('data-advertisement-id', '')
            self.card = ident if re.fullmatch(r'[1-9][0-9]{0,11}', ident) else None
            if self.card:
                if self.card in self.rows:
                    raise ValueError('duplicate_preview_identity')
                self.rows[self.card] = {}
        if self.card and tag == 'li' and 'item-char' in classes and not hidden:
            self.parts, self.kind = [], None
        if self.parts is not None and tag == 'i' and not hidden:
            kinds = {'icon-fuel': 'fuel', 'icon-akp': 'transmission', 'icon-mileage': 'mileage'}
            matches = [value for key, value in kinds.items() if key in classes]
            if len(matches) == 1:
                self.kind = matches[0]

    def handle_data(self, text):
        if (self.parts is not None and self.kind and not (self.stack and self.stack[-1][1])
                and sum(map(len, self.parts)) < 200):
            self.parts.append(text)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == 'li' and self.parts is not None:
            if self.card and self.kind:
                text = ' '.join(''.join(self.parts).split())
                if self.kind in self.rows[self.card]:
                    self.rows[self.card][self.kind] = None
                else:
                    self.rows[self.card][self.kind] = text if len(text) <= 100 else None
            self.parts, self.kind = None, None
        if tag == 'section':
            self.card = None


def parse_previews(html):
    if not isinstance(html, str) or len(html.encode('utf-8')) > 2500000:
        raise ValueError('preview_html_oversize')
    parser = PreviewParser()
    parser.feed(html)
    return parser.rows


def reject_reason(preview, subject):
    """Known mismatches only. Passing is not a valuation or match proof."""
    fuels = {'Дизель', 'Бензин', 'Газ', 'Газ / Бензин', 'Електро',
             'Гібрид (HEV)', 'Гібрид (PHEV)', 'Гібрид (MHEV)'}
    fuel = (preview.get('fuel') or '').split(',', 1)[0].strip()
    if fuel in fuels and subject.get('fuel') in fuels and fuel != subject['fuel']:
        return 'preview_fuel_mismatch'
    transmissions = {'Автомат', 'Робот', 'Ручна / Механіка', 'Варіатор'}
    transmission = preview.get('transmission')
    if (transmission in transmissions and subject.get('transmission') in transmissions
            and transmission != subject['transmission']):
        return 'preview_transmission_mismatch'
    mileage = re.fullmatch(r'(\d+(?:[.,]\d+)?) тис\. км', preview.get('mileage') or '')
    if mileage and subject.get('mileage_km') is not None:
        km = Decimal(mileage[1].replace(',', '.')) * 1000
        target = Decimal(str(subject['mileage_km']))
        if abs(km-target) > max(Decimal(50000), target*Decimal('.35')):
            return 'preview_mileage_mismatch'
    return None
