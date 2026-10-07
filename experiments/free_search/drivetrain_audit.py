"""Supplemental source-label comparability audit, not a model-policy change.

Unknown drive is not agreement. A known drive mismatch is a comparator concern,
not a ban on sending a whole vehicle under the user's production filters.
"""
from .public_detail_crosscheck import crosscheck_details

KNOWN = frozenset({'повний','передній','задній'})


def drivetrain_evidence(html, expected_id):
    data=crosscheck_details(html,expected_id)
    text=data.get('drive_text')
    label=' '.join(text.casefold().split()) if isinstance(text,str) else None
    return {'listing_id':expected_id,'drive':label if label in KNOWN else None,
            'source_assertion':True,'independently_verified':False}


def audit_drivetrain(subject, peers):
    ident=subject.get('listing_id');drive=subject.get('drive')
    if not isinstance(ident,str) or not ident.isascii() or not ident.isdigit():
        raise ValueError('invalid_subject_identity')
    rows=[];seen=set()
    for p in peers:
        pid=p.get('listing_id')
        if not isinstance(pid,str) or not pid.isascii() or not pid.isdigit() or pid in seen or pid==ident or len(rows)>=200:
            raise ValueError('invalid_or_duplicate_peer_identity')
        seen.add(pid);pd=p.get('drive')
        relation='unknown' if drive not in KNOWN or pd not in KNOWN else 'same_source_label' if drive==pd else 'known_label_conflict'
        rows.append({'listing_id':pid,'relation':relation})
    return {'subject_id':ident,'rows':rows,
            'same_count':sum(r['relation']=='same_source_label' for r in rows),
            'conflict_count':sum(r['relation']=='known_label_conflict' for r in rows),
            'unknown_count':sum(r['relation']=='unknown' for r in rows),
            'policy_changed':False,'exclude_whole_vehicle':False,
            'all_features_compatible_proven':False,'ready_for_delivery':False}


def same_drive_scenario(subject, peers, subject_evidence, peer_evidence, as_of):
    """Explicit diagnostic subset, preserving the unchanged min5 estimator.

    This is NOT the default policy and does not approve a replacement formula.
    All vehicle records remain available outside this valuation-only scenario.
    """
    from .valuation import estimate
    peers=list(peers);peer_evidence=list(peer_evidence)
    ids=[p.get('listing_id') for p in peers]
    if len(set(ids))!=len(ids) or subject_evidence.get('listing_id')!=subject.get('listing_id'):
        raise ValueError('feature_evidence_identity_mismatch')
    if any(e.get('listing_id') not in ids for e in peer_evidence):
        raise ValueError('unplanned_feature_evidence')
    # Validate duplicate/malformed evidence even when not used in a quote.
    audit_drivetrain(subject_evidence,peer_evidence)
    evidence={e['listing_id']:e for e in peer_evidence}
    audit=audit_drivetrain(subject_evidence,[evidence.get(i,{'listing_id':i,'drive':None}) for i in ids])
    same={r['listing_id'] for r in audit['rows'] if r['relation']=='same_source_label'}
    out=estimate(subject,[p for p in peers if p['listing_id'] in same],as_of).as_dict()
    return {'mode':'diagnostic_same_drive_v1','audit':audit,'outcome':out,
            'default_policy_changed':False,'replacement_formula_approved':False,
            'listing_filter_changed':False,'ready_for_delivery':False}
