import json
from pathlib import Path

from .release_gate import evaluate


def test_saved_real_release_gate_blocks_owner_canary():
    evidence=json.loads((Path(__file__).parent/'examples'/'morning-readiness-20261005.json').read_text())
    assert evaluate(evidence['facts'])==evidence['gate_result']
    assert evidence['gate_result']['technical_ready'] is False
    assert evidence['gate_result']['ready_for_owner_canary'] is False
    assert evidence['gate_result']['ready_for_client_rollout'] is False
    assert 'insufficient_compatible_analogs' in evidence['gate_result']['blockers']
    assert 'no_independent_sale_or_profit_labels' in evidence['gate_result']['blockers']


def test_passing_technical_facts_still_require_explicit_owner_canary_authorization():
    facts={
        'source':{'regular_channel_verified':True,'first_publication_verified':True},
        'market':{'minimum_required':8,'maximum_comparables':8,'total':20,'estimated':5},
        'holdout':{'estimated':3,'independently_labeled':3,'sale_or_profit_accuracy_verified':True},
        'flow':{'paid_active_stop_guard_verified':True,'unpaid_cards':0,'restart_duplicates':0,
                'real_telegram_calls':0},
        'review':{'prepared':True},
        'authorization':{'owner_canary':False,'client_rollout':False}}
    result=evaluate(facts)
    assert result['technical_ready'] is True
    assert result['ready_for_owner_canary'] is False
    assert result['blockers']==['owner_canary_not_authorized']
