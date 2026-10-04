"""Reproducible, predeclared SYNTHETIC stress comparison of three price methods.

Run: python3 -m experiments.olx_offline.olx_quality_20261004
No reads, fetching, database, model fitting or transport. Output goes to stdout.
The assumed reference values below are simulation oracles, never real valuations.
They are withheld from estimate_usd. This is not independent human validation or
proof of accuracy on OLX. Preserve real-world launch blockers despite these counts.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

from .market import METHODS, estimate_usd


NOW = 1791104400  # Fixed 2026-10-04 timestamp; reproducible, never "now".
THRESHOLD = Decimal('10')
MINIMUM = 8
NEAR = ('9800', '9850', '9900', '9950', '10000', '10000',
        '10000', '10050', '10100', '10150', '10200', '10200')

# Frozen in source before evaluating; no fitted thresholds and no winner search.
# All assumed_market_usd values are fictional independent scenario assumptions.
SCENARIOS = (
    {'id': 'clean_positive', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'near'},
    {'id': 'clean_negative', 'price': '9700', 'assumed_market_usd': '10000', 'peers': 'near'},
    {'id': 'positive_near_threshold', 'price': '8950', 'assumed_market_usd': '10000', 'peers': 'near'},
    {'id': 'negative_near_threshold', 'price': '9050', 'assumed_market_usd': '10000', 'peers': 'near'},
    {'id': 'inflated_asking_consensus', 'price': '10000', 'assumed_market_usd': '10000', 'peers': 'inflated'},
    {'id': 'depressed_asking_consensus', 'price': '8800', 'assumed_market_usd': '10000', 'peers': 'depressed'},
    {'id': 'similarity_helpful', 'price': '8800', 'assumed_market_usd': '10000', 'peers': 'weighted_low'},
    {'id': 'similarity_misleading', 'price': '9200', 'assumed_market_usd': '10000', 'peers': 'weighted_high'},
    {'id': 'lower_quartile_resists_skew', 'price': '9500', 'assumed_market_usd': '10000', 'peers': 'high_skew'},
    {'id': 'one_extreme_outlier', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'outlier'},
    {'id': 'customs_and_parts_contamination', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'ineligible'},
    {'id': 'deposit_contamination', 'price': '9500', 'assumed_market_usd': '10000', 'peers': 'deposit'},
    {'id': 'too_few_independent_vehicles', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'duplicates'},
    {'id': 'stale_peers', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'stale'},
    {'id': 'missing_gearbox', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'near', 'transmission': None},
    {'id': 'wide_price_dispersion', 'price': '8000', 'assumed_market_usd': '10000', 'peers': 'wide'},
    {'id': 'repair_cost_unobserved', 'price': '8000', 'assumed_market_usd': None, 'peers': 'unknown_condition', 'condition': None},
)


def _car(identity, price, **changes):
    row = {
        'source': 'olx', 'id': identity, 'price': str(price), 'currency': 'USD',
        'category': 'whole_passenger_car', 'price_kind': 'full',
        'price_review': {'status': 'structured_full_price', 'reasons': []},
        'eligibility_review': {'status': 'allowed'},
        'usd_price': {'status': 'ready', 'original_amount': str(price),
                      'original_currency': 'USD', 'usd_amount': str(price), 'fx': None},
        'brand': 'SyntheticBrand', 'model': 'SyntheticModel', 'generation': 'G1',
        'year': 2016, 'engine_cc': 1400, 'transmission': 'manual', 'body': 'hatchback',
        'fuel': 'petrol', 'mileage_km': 120000, 'region': 'SyntheticRegion',
        'condition': 'undamaged', 'trim': 'SyntheticTrim', 'checked_at': NOW - 100,
        'vehicle_key': 'verified-synthetic-' + identity, 'evidence': 'synthetic_only',
    }
    row.update(changes)
    return row


def _peers(kind):
    rows = [_car('peer-' + str(i), price) for i, price in enumerate(NEAR)]
    if kind == 'inflated':
        return [_car('peer-' + str(i), 12800 + i * 50) for i in range(12)]
    if kind == 'depressed':
        return [_car('peer-' + str(i), 7800 + i * 40) for i in range(12)]
    if kind in ('weighted_low', 'weighted_high'):
        close_price = '10000' if kind == 'weighted_low' else '11000'
        return ([_car('close-' + str(i), close_price) for i in range(4)] +
                [_car('far-' + str(i), '9000', year=2015, mileage_km=90000,
                      checked_at=NOW - 20 * 86400, region='OtherSyntheticRegion') for i in range(8)])
    if kind == 'high_skew':
        return [_car('peer-' + str(i), '10000' if i < 4 else '11000') for i in range(12)]
    if kind == 'outlier':
        rows.append(_car('outlier', '999999'))
    elif kind == 'ineligible':
        rows.extend(_car('forbidden-' + str(i), '1000',
                         eligibility_review={'status': 'excluded', 'reasons': [
                             'uncustoms' if i % 2 else 'parts']}) for i in range(24))
    elif kind == 'deposit':
        rows.extend(_car('deposit-' + str(i), '50000', price_kind='deposit') for i in range(24))
    elif kind == 'duplicates':
        for row in rows:
            row['vehicle_key'] = 'verified-synthetic-one-car'
            row['price'] = row['usd_price']['original_amount'] = row['usd_price']['usd_amount'] = '10000'
    elif kind == 'stale':
        for row in rows:
            row['checked_at'] = NOW - 31 * 86400
    elif kind == 'wide':
        return [_car('peer-' + str(i), price) for i, price in enumerate(
            (5000, 6000, 7000, 8000, 10000, 12000, 14000, 16000))]
    elif kind == 'unknown_condition':
        for row in rows:
            row['condition'] = None
    elif kind != 'near':
        raise ValueError('Unknown fixed scenario')
    return rows


def _truth(spec):
    market = spec['assumed_market_usd']
    if market is None:
        return 'unknown'
    value = Decimal(market)
    return 'yes' if (value - Decimal(spec['price'])) / value * 100 >= THRESHOLD else 'no'


def report():
    inputs = []
    for spec in SCENARIOS:
        changes = {key: spec[key] for key in ('transmission', 'condition') if key in spec}
        inputs.append((_car('target-' + spec['id'], spec['price'], **changes), _peers(spec['peers'])))
    baseline = deepcopy(inputs)
    counts = {m: Counter(tp=0, fp=0, tn=0, fn=0, valuation_unknown=0,
                         positive_unknown=0, unlabeled_recommendations=0,
                         truth_unknown=0) for m in METHODS}
    examples = []
    for spec, (target, peers) in zip(SCENARIOS, inputs):
        truth = _truth(spec)
        case = {'id': spec['id'], 'synthetic_truth': truth,
                'assumed_market_usd': spec['assumed_market_usd'], 'target_usd': spec['price'], 'methods': {}}
        for method in METHODS:
            # Neither synthetic_truth nor assumed_market_usd is an API input.
            result = estimate_usd(target, peers, NOW, method, MINIMUM, comparable_sources=('olx',))
            estimated = result['status'] == 'experimental_estimate'
            decision = estimated and Decimal(result['discount_percent']) >= THRESHOLD
            metric = counts[method]
            if not estimated:
                metric['valuation_unknown'] += 1
                metric['positive_unknown'] += int(truth == 'yes')
            if truth == 'unknown':
                metric['truth_unknown'] += 1
                metric['unlabeled_recommendations'] += int(decision)
            elif estimated:
                metric[('tp' if truth == 'yes' else 'fp') if decision else ('fn' if truth == 'yes' else 'tn')] += 1
            case['methods'][method] = {
                'status': result['status'], 'sample': result['sample'],
                'reference_usd': result['reference_usd'], 'discount_percent': result['discount_percent'],
                'threshold_simulation_recommended': bool(decision), 'reason': result['reason'],
                'exclusions': result['exclusions'],
            }
        examples.append(case)
    if inputs != baseline:
        raise AssertionError('Estimation mutated held-out inputs')
    positives = sum(_truth(s) == 'yes' for s in SCENARIOS)
    metrics = {}
    for method, metric in counts.items():
        evaluated = sum(metric[k] for k in ('tp', 'fp', 'tn', 'fn'))
        proposed = metric['tp'] + metric['fp']
        metrics[method] = {
            **dict(metric), 'known_truth_evaluated': evaluated,
            'positive_scenario_count': positives,
            'positive_not_recommended_total': positives - metric['tp'],
            'precision_on_labeled_synthetic_proposals': metric['tp'] / proposed if proposed else None,
            'end_to_end_synthetic_positive_recall': metric['tp'] / positives if positives else None,
        }
    blueprint = json.dumps(SCENARIOS, sort_keys=True, separators=(',', ':')).encode()
    return {
        'evidence_kind': 'synthetic_only', 'scenario_count': len(SCENARIOS),
        'scenario_blueprint_sha256': hashlib.sha256(blueprint).hexdigest(),
        'ground_truth': 'Fictional market assumptions frozen in source; withheld from the estimator.',
        'independent_real_labels': 0, 'real_peer_samples': 0, 'real_world_accuracy_verified': False,
        'source_requests': 0, 'ria_paid_operations': 0, 'telegram_sends': 0,
        'threshold_percent': str(THRESHOLD), 'minimum_peer_ads': MINIMUM, 'comparable_sources': ['olx'],
        'method_selection': {'selected_method': 'lower_quartile', 'is_provisional': True,
                             'changed_by_this_report': False,
                             'reason': 'Keep the explicit conservative research baseline; synthetic counts cannot validate real OLX accuracy.'},
        'metrics': metrics, 'scenarios': examples,
        'limitations': [
            'Scenario construction and oracle labels have one author; no independent human truth verification.',
            'No sale-price evidence, observed true market values or representative real OLX peer cohorts.',
            'Consistently inflated or depressed asking prices can defeat every method.',
            'Q25 trades fewer false positives in some scenarios for more missed positives; similarity weights can help or mislead.',
            'Threshold decisions here are simulated; estimate_usd recommended stays None and no delivery occurs.',
        ],
    }


if __name__ == '__main__':
    print(json.dumps(report(), ensure_ascii=False, indent=2))
