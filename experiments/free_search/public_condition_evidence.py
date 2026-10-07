"""Source assertions about condition, never repair-cost or bargain verification.

Past accident is distinct from current unrepaired damage. Absence is unknown;
no condition assertion here excludes a whole vehicle or approves delivery.
"""
from .public_details import parse_public_details
from .public_detail_crosscheck import _Fields

FIELDS=frozenset({'badgesDamaged','descTechStateValue'})


def condition_evidence(html, expected_id):
    parse_public_details(html, expected_id)  # ID and primary Vehicle binding.
    parser=_Fields(FIELDS);parser.feed(html)
    values={};issues=[]
    for key,blocks in parser.values.items():
        if len(blocks)!=1:
            issues.append('ambiguous_'+key);continue
        text=' '.join(''.join(blocks[0]).split())
        if len(text)>500:
            issues.append('oversize_'+key);continue
        values[key]=text.casefold()
    history=True if values.get('badgesDamaged') in {'був у дтп','був в дтп'} else None
    technical=values.get('descTechStateValue')
    repair=True if technical=='потребує ремонту' else False if technical=='не потребує ремонту' else None
    return {'listing_id':expected_id,'accident_history_asserted':history,
            'current_repair_need_asserted':repair,
            'inspection_offered':technical=='продавець готовий до перевірки на сто',
            'condition_verified':False,'repair_cost':None,'condition_adjusted':False,
            'exclude_whole_vehicle':False,'ready_for_delivery':False,'issues':issues}
