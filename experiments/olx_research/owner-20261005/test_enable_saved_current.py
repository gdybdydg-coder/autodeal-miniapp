"""New full-page observations are evidence, not permission-based readiness."""
import json
from pathlib import Path
from backend.olx_market.candidates import filter_reasons
from backend.olx_market.fx_policy import Quote
from backend.olx_market.valuation import estimate
from backend.olx_market.evaluation import evaluate_holdout

P = Path(__file__).parent


def observations():
    cars = json.loads((P / 'fresh-details-sanitized.json').read_text())
    cars += json.loads((P / 'enable-new-details-sanitized.json').read_text())
    review = json.loads((P / 'enable-current-review.json').read_text())
    earlier = json.loads((P / 'current-evidence.json').read_text())
    plan = json.loads((P / 'source-plan.json').read_text())
    return cars, review, Quote.restore(earlier['quote']), plan


def test_actual_vinnytsia_region_does_not_override_owner_price_limit():
    cars, review, quote, plan = observations()
    target = next(c for c in cars if c['id'] == '936387379')
    check = filter_reasons(target, plan['owner_search']['filters'], quote, review['checked_at'])
    assert target['price'] == '8500' and target['power_hp'] is None
    assert check['match'] is False and check['reasons'] == ['filter_price_max']
    assessed = estimate(target, cars, quote, review['checked_at'], minimum=8)
    assert assessed['reference_usd'] is None and assessed['methods'] == {}
    assert 'missing_power_hp' in assessed['reasons']


def test_actual_complete_new_ad_still_needs_eight_compatible_references():
    cars, review, quote, _ = observations()
    target = next(c for c in cars if c['id'] == '936646018')
    assessed = estimate(target, cars, quote, review['checked_at'], minimum=8)
    # This fixture contains 16 refreshed full cards; global74 also includes
    # one older compatible card. Neither denominator provides eight references.
    assert assessed['sample'] == 0 and assessed['required_sample'] == 8
    assert assessed['reference_usd'] is None and assessed['methods'] == {}
    assert assessed['reasons'] == ['insufficient_comparables']


def test_new_references_do_not_turn_prior_controls_into_reference_cars():
    cars, review, quote, _ = observations()
    old = json.loads((P / 'split-freeze.json').read_text())['membership']
    supplement = json.loads((P / 'enable-detail-freeze.json').read_text())['membership']
    split = old | supplement
    controls = {id for id, partition in split.items() if partition == 'holdout'}
    actual = evaluate_holdout(cars, split, quote, review['checked_at'], minimum=8)
    assert actual['frozen_holdout_count'] == 3 and actual['estimated_holdout_count'] == 0
    assert actual['false_positive'] is None and actual['false_negative'] is None
    assert actual['winner_selected'] is None
    assert all(controls.isdisjoint(row['comparison_group_ids']) for row in actual['rows'])
