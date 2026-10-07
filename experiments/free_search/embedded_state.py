"""Decode a bounded JSON assignment in supplied HTML; never execute scripts."""
import json
from html.parser import HTMLParser

class EmbeddedStateError(ValueError):
    pass

class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(); self.active=False; self.scripts=[]
    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active=True; self.scripts.append('')
    def handle_endtag(self, tag):
        if tag == 'script': self.active=False
    def handle_data(self, value):
        if self.active: self.scripts[-1] += value

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise EmbeddedStateError('duplicate_json_key')
        result[key]=value
    return result

def _invalid_constant(value):
    raise EmbeddedStateError('invalid_json_number')

def parse_embedded_state(html):
    if not isinstance(html,str) or len(html.encode()) > 2_500_000:
        raise EmbeddedStateError('html_oversize')
    parser=_Scripts(); parser.feed(html)
    marker='window.__PINIA__ = '
    matches=[s for s in parser.scripts if marker in s]
    if len(matches)!=1 or matches[0].count(marker)!=1:
        raise EmbeddedStateError('state_missing_or_ambiguous')
    raw=matches[0].split(marker,1)[1]
    try:
        state,end=json.JSONDecoder(object_pairs_hook=_pairs,parse_constant=_invalid_constant).raw_decode(raw)
        if raw[end:].strip()!=';' or not isinstance(state,dict):
            raise EmbeddedStateError('invalid_state')
        return state
    except (ValueError,RecursionError) as exc:
        if isinstance(exc,EmbeddedStateError): raise
        raise EmbeddedStateError('invalid_state') from exc
