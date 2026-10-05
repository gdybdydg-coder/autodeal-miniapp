"""Saved real Oct5 receipts, not live sends, synthetic cars or sale labels."""
import json
from pathlib import Path
from backend.olx_market.valuation import estimate
from backend.olx_market.evaluation import evaluate_holdout
from backend.olx_market.candidates import filter_reasons
from backend.olx_market.fx_policy import Quote
P=Path(__file__).parent

def data():
    return json.loads((P/'fresh-details-sanitized.json').read_text()),json.loads((P/'current-evidence.json').read_text()),json.loads((P/'source-plan.json').read_text())

def test_real_matching_khmelnytskyi_ad_never_receives_fabricated_value():
    cars,e,p=data();now=e['checked_at'];quote=Quote.restore(e['quote'])
    target=next(c for c in cars if c['id']=='936658970')
    assert target['region']=='Хмельницкая область'
    assert filter_reasons(target,p['owner_search']['filters'],quote,now)['match']
    a=estimate(target,cars,quote,now,minimum=8)
    assert target['research_condition']=='running_reported_damage:body_dents+windshield_crack'
    assert a['sample']==0 and a['methods']=={} and a['reference_usd'] is None
    assert a['status']=='profitability_unconfirmed'
    assert a['reasons']==['insufficient_comparables']

def test_real_missing_power_is_unknown_not_a_guessed_105():
    cars,e,p=data();target=next(c for c in cars if c['id']=='935196654')
    assert target['power_hp'] is None
    a=estimate(target,cars,Quote.restore(e['quote']),e['checked_at'],minimum=8)
    assert a['methods']=={} and 'missing_power_hp' in a['reasons']

def test_saved_real_frozen_controls_keep_zero_coverage_and_no_winner():
    cars,e,p=data();f=json.loads((P/'split-freeze.json').read_text())
    r=evaluate_holdout(cars,f['membership'],Quote.restore(e['quote']),e['checked_at'])
    assert (r['frozen_holdout_count'],r['eligible_holdout_count'],r['estimated_holdout_count'])==(3,2,0)
    assert r['winner_selected'] is None and r['false_positive'] is None and r['false_negative'] is None
    assert all(m['mean_absolute_percent'] is None for m in r['metrics'].values())
