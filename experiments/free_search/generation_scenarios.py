"""Conditional peer-group diagnostics when the target generation is unknown.

Never fill a generation from year, popularity, price or the dominant peer group.
A conditional group estimate is not an unconditional target valuation.
"""
from .valuation import estimate


def _label(value):
    return ' '.join(value.split()) if isinstance(value,str) and value.strip() and len(value)<=200 else None


def unknown_generation_scenarios(subject, peers, as_of):
    if _label(subject.get('generation')) is not None:
        raise ValueError('target_generation_already_known')
    peers=list(peers)
    if len(peers)>200:raise ValueError('peer_budget_exceeded')
    groups={};missing=0
    for p in peers:
        label=_label(p.get('generation'))
        if label is None:missing+=1
        else:groups.setdefault(label,[]).append(p)
    cases=[]
    for label,rows in sorted(groups.items()):
        # Keep subject generation unknown: this does not invent a source field.
        cases.append({'peer_generation':label,'input_count':len(rows),
                      'condition':'target must independently be verified as this generation',
                      'outcome':estimate(subject,rows,as_of).as_dict()})
    return {'cases':cases,'unknown_peer_generation_count':missing,
            'target_generation_inferred':False,'unconditional_reference_price':None,
            'requires_target_generation_verification':True,'policy_changed':False,
            'exclude_whole_vehicle':False,'ready_for_delivery':False}
