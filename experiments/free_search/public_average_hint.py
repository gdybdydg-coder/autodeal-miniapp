"""Public average-price UI hint, explicitly NOT an approved market quote.

No currency/range/sample period can be inferred from a nearby asking price,
paid logs or the fact that some values coincide. The function has no IO.
"""
import math
import re
from .embedded_state import parse_embedded_state


def extract_average_hint(html, expected_id):
    if not isinstance(expected_id,str) or not re.fullmatch(r'[1-9][0-9]{0,11}',expected_id):
        raise ValueError('invalid_expected_id')
    state=parse_embedded_state(html)
    page=state.get('page',{})
    path=page.get('path')
    if not isinstance(path,str) or not re.fullmatch(r'/(?:uk/)?auto_[a-z0-9_-]*_'+expected_id+r'\.html/?',path):
        raise ValueError('page_identity_mismatch')
    root=page.get('structures',{}).get(path)
    if not isinstance(root,dict): raise ValueError('primary_structure_missing')
    stack=[root]; candidates=[]; visited=0
    while stack:
        node=stack.pop(); visited+=1
        if visited>10000: raise ValueError('too_many_templates')
        if not isinstance(node,dict): continue
        # Only the primary template tree, not apiData, cloned registry or ads.
        if node.get('isHide') is True: continue
        if node.get('id')=='basicInfoPriceGhost':
            action=node.get('actionData',{})
            if (node.get('action')!='showBottomPopUp' or
                type(action.get('autoId')) is not int or str(action['autoId'])!=expected_id or
                action.get('blockId')!='averagePrice'):
                raise ValueError('average_identity_mismatch')
            value=action.get('params',{}).get('averagePrice')
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<1e9:
                raise ValueError('invalid_average')
            copies=[p[1] for p in action.get('data',[]) if isinstance(p,list) and len(p)==2 and p[0]=='averagePrice']
            if copies and (len(copies)!=1 or str(copies[0])!=str(value)):
                raise ValueError('conflicting_average')
            candidates.append(value)
        children=node.get('templates',[])
        if not isinstance(children,list): raise ValueError('invalid_templates')
        stack.extend(children)
    if len(candidates)>1: raise ValueError('ambiguous_average')
    return {'id':expected_id,'status':'hint_only' if candidates else 'unavailable',
            'average_raw':candidates[0] if candidates else None,'currency':None,
            'lower_bound':None,'upper_bound':None,'source_as_of':None,
            'sample_count':None,'period_hours':None,'market':None,
            'formula_equivalence_verified':False,'ready_for_delivery':False,
            'reason':'public_average_missing_range_currency_and_method' if candidates else 'public_average_missing'}
