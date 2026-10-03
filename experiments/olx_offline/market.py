"""Pure, experimental OLX asking-price comparison; no I/O or production imports.

Inputs must have fresh eligibility, full-price and USD normalization reviews.
``vehicle_key`` is an upstream VERIFIED vehicle identity, never a key invented
from title/year/price. Without it, distinct advertisements are not proof of
distinct vehicles: that limitation is reported. Methods are hypotheses, not
validated resale appraisals. The default Q25 choice is explicit and provisional.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

from .fx import DECIMAL_PRECISION, FXQuote, day, instant, quote_problem


DAY = 86400
MAX_AGE = 30 * DAY
METHODS = ('robust_median', 'lower_quartile', 'weighted_similar')
REQUIRED = ('brand', 'model', 'generation', 'body', 'fuel', 'transmission', 'engine_cc')
DESCRIPTIONS = {
    'robust_median': 'Median of matched asking prices after an inclusive-IQR outlier screen.',
    'lower_quartile': 'Inclusive 25th percentile of matched asking prices; no extra percentage margin.',
    'weighted_similar': 'Weighted median: closer year/mileage, fresher observations and same region receive more weight.',
}


def _decimal(value):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _text(value):
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _identity(car):
    source, identity = car.get('source'), car.get('id')
    return (source, identity) if source in ('olx', 'auto_ria') and isinstance(identity, str) and identity else None


def _condition(car):
    value = _text(car.get('condition'))
    if value in ('damaged', 'пошкоджений', 'поврежденный', 'битий', 'битый'):
        return 'damaged'
    if value in ('not_running', 'не на ходу', 'non_running'):
        return 'not_running'
    if value in ('good', 'normal', 'справний', 'исправный', 'без пошкоджень', 'undamaged'):
        return 'undamaged'
    if value in (None, 'unknown', 'невідомо'):
        return None
    # Unmapped descriptions must not silently join the undamaged cohort.
    return 'explicit:' + value


def _percentile(values, proportion):
    ordered = sorted(values)
    index = Decimal(len(ordered) - 1) * proportion
    low = int(index)
    fraction = index - low
    return ordered[low] if not fraction else ordered[low] + (ordered[low + 1] - ordered[low]) * fraction


def _usd(car, now):
    """Validate the supplied normalization proof; never invent an FX rate.

    Re-run the shared FX quote policy at evaluation time so an earlier ready
    proof does not survive a Kyiv date change or cache expiry. All UAH analogs
    must subsequently share one quote basis.
    """
    proof = car.get('usd_price')
    if not isinstance(proof, dict) or proof.get('status') != 'ready':
        return None, None, 'usd_normalization_unconfirmed'
    amount, original = _decimal(proof.get('usd_amount')), _decimal(proof.get('original_amount'))
    if amount is None or original is None or not isinstance(proof.get('usd_amount'), str):
        return None, None, 'invalid_usd_price'
    currency = proof.get('original_currency')
    if currency != car.get('currency') or original != _decimal(car.get('price')):
        return None, None, 'usd_original_price_conflict'
    fx = proof.get('fx')
    if currency == 'USD':
        if fx is not None or amount != original:
            return None, None, 'double_conversion_or_usd_conflict'
        return amount, None, None
    if currency != 'UAH' or not isinstance(fx, dict):
        return None, None, 'unsupported_or_unproven_currency'
    rate = _decimal(fx.get('uah_per_usd'))
    source, effective, fetched = fx.get('source'), fx.get('effective_date'), fx.get('fetched_at')
    if rate is None or not isinstance(source, str) or not source or not isinstance(effective, str):
        return None, None, 'incomplete_fx_provenance'
    try:
        quote = FXQuote(rate, day(effective), instant(fetched), source,
                        day(fx['calculated_on']) if fx.get('calculated_on') else None)
        problem = quote_problem(quote, now)
    except (TypeError, ValueError, OverflowError):
        return None, None, 'invalid_fx_provenance'
    if problem:
        return None, None, problem
    # Decimal normalization can use a different precision. This numerical-only
    # tolerance is far below a cent and never rounds a price for comparison.
    expected = original / rate
    if abs(amount - expected) > max(abs(expected), Decimal(1)) * Decimal('1e-24'):
        return None, None, 'fx_arithmetic_conflict'
    return amount, (source, effective, str(rate.normalize())), None


def _screen(car, now, max_age):
    if not isinstance(car, dict) or _identity(car) is None:
        return None, None, 'invalid_listing_identity'
    review = car.get('eligibility_review')
    if not isinstance(review, dict) or review.get('status') != 'allowed':
        return None, None, 'eligibility_not_allowed'
    if car.get('category') != 'whole_passenger_car':
        return None, None, 'not_a_whole_passenger_car'
    if car.get('price_kind') != 'full':
        return None, None, 'full_price_unconfirmed'
    price_review = car.get('price_review')
    if not isinstance(price_review, dict) or price_review.get('status') != 'structured_full_price' or price_review.get('reasons'):
        return None, None, 'price_evidence_conflict'
    if any(car.get(key) for key in ('price_conflicts', 'field_conflicts')):
        return None, None, 'structured_evidence_conflict'
    if any(_text(car.get(k)) is None for k in REQUIRED if k != 'engine_cc'):
        return None, None, 'missing_comparison_attributes'
    for key, low, high in (('engine_cc', 1, 20000), ('year', 1886, 2100), ('mileage_km', 0, 10**7)):
        if type(car.get(key)) is not int or not low <= car[key] <= high:
            return None, None, 'missing_comparison_attributes'
    checked = car.get('checked_at')
    if type(checked) is not int or not 0 <= now - checked <= max_age:
        return None, None, 'stale_or_future_observation'
    return _usd(car, now)


def _unknown(reason, method, *, sample=0, exclusions=None):
    return {'status': 'profitability_unconfirmed', 'reason': reason, 'reasons': [reason],
            'sample': sample, 'currency': 'USD', 'method': method,
            'reference_usd': None, 'range_usd': None, 'discount_percent': None,
            'reliability': 'insufficient', 'real_world_accuracy_verified': False,
            'exclusions': dict(exclusions or {}), 'next_action': 'Obtain or verify eligible independent comparable evidence.'}


def _estimate(target, comparables, now, method, minimum, max_age):
    amount, target_fx, error = _screen(target, now, max_age)
    if error:
        return _unknown(error, method)
    target_id = _identity(target)
    target_vehicle = _text(target.get('vehicle_key'))
    condition = _condition(target)
    exclusions = Counter()
    rows = []
    # Choose latest observation per source+ID before comparing prices; never let
    # an old cheap version of an ad inflate the sample. Same-time conflicts are
    # unresolved and the whole identity is held out.
    by_id = {}
    for candidate in comparables:
        if not isinstance(candidate, dict) or _identity(candidate) is None:
            exclusions['invalid_listing_identity'] += 1
            continue
        by_id.setdefault(_identity(candidate), []).append(candidate)
    # A known crosspost cannot launder a current exclusion through an older
    # eligible card of the same verified car. Review the latest vehicle-level
    # evidence before dropping any individual forbidden candidate.
    vehicle_observations = {}
    for versions in by_id.values():
        for candidate in versions:
            key = _text(candidate.get('vehicle_key'))
            if key and type(candidate.get('checked_at')) is int:
                vehicle_observations.setdefault(key, []).append(candidate)
    held_vehicles = set()
    for key, versions in vehicle_observations.items():
        latest_at = max(v['checked_at'] for v in versions)
        latest_versions = [v for v in versions if v['checked_at'] == latest_at]
        if any(not isinstance(v.get('eligibility_review'), dict) or
               v['eligibility_review'].get('status') != 'allowed' for v in latest_versions):
            held_vehicles.add(key)
    for identity, versions in sorted(by_id.items()):
        if identity == target_id:
            exclusions['self_or_verified_duplicate'] += len(versions)
            continue
        dated = [v for v in versions if type(v.get('checked_at')) is int]
        if not dated:
            exclusions['stale_or_future_observation'] += len(versions)
            continue
        latest = max(v['checked_at'] for v in dated)
        newest = [v for v in dated if v['checked_at'] == latest]
        if any(v != newest[0] for v in newest[1:]):
            exclusions['conflicting_duplicate_observation'] += len(versions)
            continue
        car = newest[0]
        exclusions['duplicate_source_id'] += len(versions) - 1
        vehicle = _text(car.get('vehicle_key'))
        if target_vehicle is not None and vehicle == target_vehicle:
            exclusions['self_or_verified_duplicate'] += 1
            continue
        if vehicle is not None and vehicle in held_vehicles:
            exclusions['verified_vehicle_current_policy_not_allowed'] += 1
            continue
        price, fx_basis, reason = _screen(car, now, max_age)
        if reason:
            exclusions[reason] += 1
            continue
        if any((_text(car[k]) != _text(target[k]) if k != 'engine_cc' else car[k] != target[k]) for k in REQUIRED):
            exclusions['incompatible_attributes'] += 1
            continue
        if abs(car['year'] - target['year']) > 1 or abs(car['mileage_km'] - target['mileage_km']) > 30000:
            exclusions['incompatible_year_or_mileage'] += 1
            continue
        if _condition(car) != condition:
            exclusions['incompatible_or_unknown_condition'] += 1
            continue
        if _text(target.get('trim')) and _text(car.get('trim')) and _text(target['trim']) != _text(car['trim']):
            exclusions['incompatible_trim'] += 1
            continue
        rows.append((car, price, fx_basis, vehicle))
    # USD-origin cars need no quote. All UAH conversions must use one quote; if
    # target is USD choose the newest available basis, never whichever yields
    # the most favorable recommendation.
    ua_bases = {r[2] for r in rows if r[2] is not None}
    basis = target_fx if target_fx is not None else max(ua_bases, key=lambda x: (x[1], x[0], x[2]), default=None)
    same_basis = []
    for row in rows:
        if row[2] is not None and row[2] != basis:
            exclusions['inconsistent_fx_basis'] += 1
        else:
            same_basis.append(row)
    rows = same_basis
    deduped, vehicles = [], set()
    for row in sorted(rows, key=lambda r: (-r[0]['checked_at'], _identity(r[0]))):
        if row[3] is not None and row[3] in vehicles:
            exclusions['duplicate_verified_vehicle'] += 1
            continue
        if row[3] is not None:
            vehicles.add(row[3])
        deduped.append(row)
    rows = deduped
    if len(rows) < minimum:
        return _unknown('insufficient_comparables', method, sample=len(rows), exclusions=exclusions)
    values = [r[1] for r in rows]
    q1, q3 = _percentile(values, Decimal('.25')), _percentile(values, Decimal('.75'))
    iqr = q3 - q1
    accepted = [r for r in rows if q1 - Decimal('1.5') * iqr <= r[1] <= q3 + Decimal('1.5') * iqr]
    exclusions['price_outlier'] += len(rows) - len(accepted)
    rows = accepted
    if len(rows) < minimum:
        return _unknown('insufficient_after_outliers', method, sample=len(rows), exclusions=exclusions)
    values = [r[1] for r in rows]
    q1, q3, center = (_percentile(values, p) for p in (Decimal('.25'), Decimal('.75'), Decimal('.5')))
    if (max(values) - min(values)) / center > Decimal('.4'):
        return _unknown('wide_price_dispersion', method, sample=len(rows), exclusions=exclusions)
    reference = center if method == 'robust_median' else q1
    if method == 'weighted_similar':
        weighted = []
        for car, value, _, _ in rows:
            distance = Decimal(abs(car['year'] - target['year'])) + Decimal(abs(car['mileage_km'] - target['mileage_km'])) / 30000
            age = Decimal(now - car['checked_at']) / max_age
            region_penalty = Decimal(0) if _text(car.get('region')) and _text(car.get('region')) == _text(target.get('region')) else Decimal('.5')
            weighted.append((value, Decimal(1) / (1 + distance + age + region_penalty)))
        halfway, cumulative = sum(w for _, w in weighted) / 2, Decimal(0)
        for value, weight in sorted(weighted):
            cumulative += weight
            if cumulative >= halfway:
                reference = value
                break
    uncertainties = ['asking_prices_not_confirmed_sales', 'method_not_real_world_validated',
                     'empirical_iqr_is_not_a_confidence_interval', 'unobserved_condition_and_options_may_change_value']
    if condition is None:
        uncertainties.append('condition_unknown_for_target_and_comparables')
    elif condition != 'undamaged':
        uncertainties.append('repair_severity_and_cost_not_verified')
    if not target_vehicle or any(r[3] is None for r in rows):
        uncertainties.append('crossposts_without_verified_vehicle_identity_may_remain')
    if not _text(target.get('region')) or any(_text(r[0].get('region')) != _text(target.get('region')) for r in rows):
        uncertainties.append('region_unknown_or_mixed')
    if not _text(target.get('trim')) or any(not _text(r[0].get('trim')) for r in rows):
        uncertainties.append('trim_not_fully_matched')
    return {'status': 'experimental_estimate', 'reason': None, 'reasons': uncertainties,
            'sample': len(rows), 'sample_unit': 'distinct_ads_after_known_vehicle_dedup',
            'reference_usd': str(reference), 'range_usd': {'low': str(q1), 'high': str(q3)},
            'range_kind': 'empirical_interquartile_asking_price_range_not_prediction_interval',
            'asking_median': str(center), 'conservative_reference': str(reference),
            'discount_percent': str((reference - amount) / reference * 100), 'currency': 'USD',
            'method': method, 'method_description': DESCRIPTIONS[method],
            'method_selection': 'experimental_choice_pending_independent_validation',
            'reliability': 'experimental_low', 'real_world_accuracy_verified': False,
            'data_checked_at': {'oldest': min(r[0]['checked_at'] for r in rows), 'newest': max(r[0]['checked_at'] for r in rows)},
            'evaluated_at': now, 'exclusions': {k: v for k, v in exclusions.items() if v},
            'used_comparables': [{'source': r[0]['source'], 'id': r[0]['id'], 'usd_amount': str(r[1]), 'checked_at': r[0]['checked_at']} for r in rows],
            'extra_margin_percent': '0', 'recommended': None,
            'next_action': 'Validate precision, missed profitable cars and unknown coverage on an independent real sample.'}


def estimate_usd(target, comparables, now, method='lower_quartile', minimum=8, *, max_age=MAX_AGE):
    """Return decimal strings; never recommend or mutate an input listing.

    These hard matching thresholds and minimum are research parameters awaiting
    calibration. Missing fuel/gear yields unresolved valuation, not filter denial.
    """
    if method not in METHODS:
        raise ValueError('Unknown market comparison method')
    if type(now) is not int or now <= 0 or type(minimum) is not int or minimum < 2:
        raise ValueError('Positive observation time and minimum >= 2 required')
    if type(max_age) is not int or max_age <= 0:
        raise ValueError('Positive maximum observation age required')
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return _estimate(target, comparables, now, method, minimum, max_age)


def compare_methods(target, comparables, now, minimum=8, *, max_age=MAX_AGE):
    """Evaluate three hypotheses on identical supplied observations, not truth.

    No winner is selected by discount size or number of suggested bargains.
    A held-out, independently labeled real sample is still required.
    """
    rows = list(comparables)
    return {'methods': {method: estimate_usd(target, rows, now, method, minimum, max_age=max_age) for method in METHODS},
            'selected_method': 'lower_quartile', 'selection_is_provisional': True,
            'real_world_accuracy_verified': False,
            'selection_reason': 'Explicit conservative research baseline; no independent accuracy evidence yet.'}
