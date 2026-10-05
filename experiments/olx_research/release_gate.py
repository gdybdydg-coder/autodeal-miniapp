"""Fail-closed OLX research release gate.

This module does not deploy, send, read production state or grant permission.
It only turns explicit evidence into a reproducible readiness decision.
"""


def evaluate(facts):
    blockers=[]
    source=facts['source'];market=facts['market'];holdout=facts['holdout']
    flow=facts['flow'];review=facts['review'];authorization=facts['authorization']
    if source.get('regular_channel_verified') is not True:
        blockers.append('regular_source_channel_unverified')
    if source.get('first_publication_verified') is not True:
        blockers.append('first_publication_unverified')
    if market.get('maximum_comparables',0)<market.get('minimum_required',8):
        blockers.append('insufficient_compatible_analogs')
    if market.get('estimated',0)<=0:
        blockers.append('zero_real_market_coverage')
    if holdout.get('estimated',0)<=0:
        blockers.append('no_estimable_holdout')
    if (holdout.get('independently_labeled',0)<=0
            or holdout.get('sale_or_profit_accuracy_verified') is not True):
        blockers.append('no_independent_sale_or_profit_labels')
    if flow.get('paid_active_stop_guard_verified') is not True:
        blockers.append('paid_active_stop_guard_unverified')
    if flow.get('unpaid_cards')!=0:
        blockers.append('unpaid_delivery_detected')
    if flow.get('restart_duplicates')!=0:
        blockers.append('restart_duplicates_detected')
    if review.get('prepared') is not True:
        blockers.append('review_package_missing')
    technical_ready=not blockers
    owner_blockers=list(blockers)
    if authorization.get('owner_canary') is not True:
        owner_blockers.append('owner_canary_not_authorized')
    client_blockers=list(owner_blockers)
    if flow.get('owner_canary_delivery_verified') is not True:
        client_blockers.append('owner_canary_delivery_not_verified')
    if authorization.get('client_rollout') is not True:
        client_blockers.append('client_rollout_not_authorized')
    return {
        'technical_ready':technical_ready,
        'ready_for_owner_canary':technical_ready and authorization.get('owner_canary') is True,
        'ready_for_client_rollout':not client_blockers,
        'blockers':owner_blockers,
        'client_rollout_blockers':client_blockers,
        'production_action':'none',
        'stage9_started':False,
        'stage10_started':False}
