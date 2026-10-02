"""Cold, synthetic, offline end-to-end comparison; never a production runner.

All source HTML, peer asking prices, labels and recipients are generated here.
The independent truth is a declared latent fixture market, not AUTO.RIA prices.
No cache, private audit data, backend setting, API key or real sender is loaded.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time

from experiments.free_search.durable_pipeline import DurablePipeline
from experiments.free_search.filter_gate import SearchFilter, Span
from experiments.free_search.network_guard import OutboundGuard
from experiments.free_search.public_cards import Candidate, parse_public_cards
from experiments.free_search.public_details import parse_public_details
from experiments.free_search.visible_adapter import adapt_candidate, parse_visible_facts
from experiments.free_search.valuation import estimate, evaluate_labeled_cases

D = Decimal
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp()
GROUPS = (
    ('Ford', 'Focus', 2015, 6000), ('Skoda', 'Octavia', 2016, 9500),
    ('Renault', 'Megane', 2018, 13000), ('Volkswagen', 'Golf', 2019, 18000),
)
REGIONS = ('Київська область', 'Вінницька область', 'Тернопільська область', 'Львівська область')


def _url(sid):
    # A synthetic identity for parser validation only. Never fetched.
    return f'https://auto.ria.com/uk/auto_synthetic_fixture_{sid}.html'


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _detail_html(car):
    node = {'@type': 'Vehicle', '@id': _url(car['listing_id']), 'url': _url(car['listing_id']),
            'brand': {'@type': 'Brand', 'name': car['brand']}, 'model': car['model'],
            'productionDate': str(car['year']), 'bodyType': car.get('body', 'Седан'),
            'mileageFromOdometer': {'value': car['mileage_km'], 'unitCode': 'KMT'},
            'offers': {'@type': 'Offer', 'price': str(car['price']), 'priceCurrency': 'USD',
                       'availability': 'https://schema.org/InStock'}}
    for field, source in [('fuelType', 'fuel'), ('vehicleTransmission', 'transmission')]:
        if car.get(source) is not None:
            node[field] = car[source]
    if car.get('damaged'):
        node['itemCondition'] = 'https://schema.org/DamagedCondition'
    day = datetime.fromtimestamp(car['published_at'], timezone.utc).strftime('%d.%m.%Y')
    return (f'<a>Легкові з пробігом</a><a>{car["region"]}</a><h1>Synthetic fixture</h1>'
            f'<p>Оголошення створене {day}</p><p>ID авто {car["listing_id"]}</p>'
            '<script type="application/ld+json">' + json.dumps(node, ensure_ascii=False) + '</script>')


def _feed_html(cars):
    return ''.join(
        f'<section class="ticket-item" data-advertisement-id="{c["listing_id"]}">'
        f'<a class="m-link-ticket" href="{_url(c["listing_id"])}">Synthetic</a>'
        f'<span data-add-date="{_stamp(c["published_at"])}" data-update-date="{_stamp(c["published_at"])}"></span>'
        f'<span class="price-ticket" data-main-currency="USD" data-main-price="{c["preview"]}"></span>'
        '</section>' for c in cars)


def make_fixture(cars=240, scenario='recovery'):
    """Truth is defined before parsing/model output and never given to estimator."""
    if type(cars) is not int or not 200 <= cars <= 1000 or scenario not in ('normal', 'recovery'):
        raise ValueError('invalid_fixture_configuration')
    targets = []
    for i in range(cars):
        group = (i // 10) % 4
        brand, model, year, market = GROUPS[group]
        slot = i % 10
        ratio = D('0.70') if slot < 5 or slot == 9 else D('1.05')
        if scenario == 'recovery' and slot == 4:
            ratio = D('0.88')  # Genuine latent discount, may be missed by lower-quartile model.
        sid = str(9100000000 + i)
        if scenario == 'recovery' and i == cars - 1:
            sid = '8100000001'  # Late appearance with lower ID, still inside measured fixture window.
        row = dict(index=i, listing_id=sid, group=group, brand=brand, model=model, year=year,
                   region=REGIONS[i % 2 + (2 if group >= 2 else 0)], body='Седан', fuel='Дизель',
                   transmission='Механічна', mileage_km=150000 + (i % 5) * 1000,
                   price=int(D(market) * ratio), preview=int(D(market) * ratio),
                   published_at=NOW - 240 + i % 200, photo=i % 3 != 0,
                   latent_market=market, should_qualify=ratio <= D('0.90'),
                   damaged=i % 13 == 0, price_kind='whole_vehicle')
        if scenario == 'recovery':
            if i % 17 == 0:
                row['preview'] = 0
            elif i % 19 == 0:
                row['preview'] = market * 4  # Fresh current detail price must win.
            if i % 11 == 0:
                row.update(fuel=None, transmission=None, photo=False)
            if slot == 8:
                row.update(price_kind='upfront', should_qualify=False)
            if i % 31 == 0:
                row['body'] = 'Універсал'  # A known contrary filter, unlike missing fields.
        targets.append(row)
    peers = []
    # A new peer corpus built from fixture HTML in this invocation, never an old paid cache.
    for group, (brand, model, year, market) in enumerate(GROUPS):
        for j in range(12):
            price = D(market) * (D('0.93') + D(j) / 100)
            if scenario == 'recovery' and group == 3:
                price *= D('1.25')  # Biased asking prices: exposes false positives vs independent latent truth.
            peers.append(dict(listing_id=str(9200000000 + group * 100 + j), group=group,
                              brand=brand, model=model, year=year,
                              region=REGIONS[j % 2 + (2 if group >= 2 else 0)],
                              body='Седан', fuel='Дизель', transmission='Механічна',
                              mileage_km=150000 + j * 1000, price=int(price), preview=int(price),
                              published_at=NOW - 86400 * (1 + j % 4), photo=False,
                              price_kind='whole_vehicle'))
    return dict(targets=targets, peers=peers, scenario=scenario)


def parse_fixture_detail(car, observed_at):
    html = _detail_html(car)
    details = parse_public_details(html, car['listing_id'])
    visible = parse_visible_facts(html, car['listing_id'])
    candidate = Candidate(car['listing_id'], _url(car['listing_id']), car['published_at'],
                          None if not car['preview'] else D(car['preview']))
    result = adapt_candidate(candidate, details, visible)
    if not result.evidence.publication_proven:
        raise ValueError('fixture_publication_not_proven')
    record = asdict(result.evidence)
    # These are explicitly supplied synthetic source annotations, not fields claimed
    # to be available from the present live parser. They never contain truth labels.
    record.update(price=str(details.price), currency=details.currency,
                  observed_at=observed_at, price_observed_at=observed_at,
                  published_at=car['published_at'], price_kind=car.get('price_kind', 'whole_vehicle'),
                  photo='https://example.invalid/synthetic.jpg' if car.get('photo') else None)
    return record


def _filters(uid):
    group = (uid - 1) % 4
    brand, model, _, market = GROUPS[group]
    regions = frozenset(REGIONS[:2] if group < 2 else REGIONS[2:])
    return SearchFilter(brand=brand, model=model, regions=regions,
                        price_usd=Span(D(market) * D('0.4'), D(market) * D('1.5')),
                        year_min=2010, year_max=2022, bodies=frozenset({'Седан'}),
                        fuels=frozenset({'Дизель'}), transmissions=frozenset({'Механічна'}),
                        mileage_max_km=200000, min_discount_percent=D('10'))


def _truth_pair(car, uid, scenario):
    # Independent oracle uses fixture cohort assignment and latent labels, never gate()/estimate().
    if not car['should_qualify'] or (uid - 1) % 4 != car['group'] or car['body'] != 'Седан':
        return False
    if scenario == 'recovery' and uid % 20 in (0, 1, 3, 4):
        return False  # stopped/expired before actual sending, including post-queue races.
    return True


def _stats(values):
    return None if not values else dict(count=len(values), minimum=min(values),
        median=statistics.median(values), maximum=max(values))


def _baseline_model(fixture, users, peers):
    """Explicit counterfactual policy model, NOT current production execution.

    Two pages x100, eager preview rejection, one-shot source/detail/valuation/send
    handling. It quantifies those restrictions only; existing production may
    already fix some. The actual parsers/estimator are shared with the new path.
    """
    source_rows = fixture['targets'][:-1] if fixture['scenario'] == 'recovery' else fixture['targets']
    selected_ids = [c.listing_id for offset in (0, 100)
                    if source_rows[offset:offset+100]
                    for c in parse_public_cards(_feed_html(source_rows[offset:offset+100]))]
    cars = [c for c in source_rows if c['listing_id'] in set(selected_ids)]
    counters = Counter(discovered=len(cars), detail_parsed=0, estimated=0, accepted_pairs=0,
                       source_cap_unseen=len(fixture['targets']) - len(cars))
    sent = set()
    for car in cars:
        risk = fixture['scenario'] == 'recovery'
        if risk and (not car['preview'] or car['preview'] > car['latent_market'] * 1.5):
            counters['preview_rejected'] += 1
            continue
        if risk and car['index'] % 23 == 0:
            counters['detail_transient_abandoned'] += 1
            continue
        details = parse_fixture_detail(car, NOW)
        counters['detail_parsed'] += 1
        if risk and car['index'] % 29 == 0:
            counters['valuation_transient_abandoned'] += 1
            continue
        if risk and car['index'] % 37 == 0:
            details = dict(details, model='Synthetic unknown model')
        outcome = estimate(details, peers, NOW)
        if outcome.status != 'estimated':
            counters['valuation_unknown'] += 1
            continue
        counters['estimated'] += 1
        if outcome.discount_percent < 10 or car['body'] != 'Седан':
            continue
        for uid in range(1, users + 1):
            if (uid - 1) % 4 != car['group']:
                continue
            if risk and uid % 20 in (0, 1, 3, 4):
                continue
            if risk and (car['index'] * 31 + uid) % 97 in (0, 1, 2, 3, 4):
                counters['known_or_ambiguous_send_not_accepted'] += 1
                continue
            sent.add((car['listing_id'], uid))
    counters['accepted_pairs'] = len(sent)
    return counters, sent


def run_benchmark(*, users=100, cars=240, scenario='recovery'):
    if type(users) is not int or not 4 <= users <= 200:
        raise ValueError('invalid_user_count')
    started = time.perf_counter()
    outer_guard = OutboundGuard()
    with outer_guard.isolated(), tempfile.TemporaryDirectory(prefix='autodeal-cold-replay-') as tmp:
        fixture = make_fixture(cars, scenario)
        by_id = {c['listing_id']:c for c in fixture['targets']}
        peers = [parse_fixture_detail(c, NOW - 10) for c in fixture['peers']]
        path = Path(tmp) / 'benchmark.free-pipeline.sqlite3'
        policy = dict(max_pending=125 if scenario == 'recovery' else cars + 10)
        p = DurablePipeline(path, **policy)
        clock = NOW
        parse_calls = Counter(); valuation_calls = Counter(); latest_outcomes = {}
        def loader(row):
            car = by_id[row['id']]; parse_calls[row['id']] += 1
            if scenario == 'recovery' and car['index'] % 23 == 0 and parse_calls[row['id']] == 1:
                raise TimeoutError('synthetic_detail_timeout')
            result = parse_fixture_detail(car, clock)
            if scenario == 'recovery' and car['index'] % 37 == 0:
                result['model'] = 'Synthetic unknown model'
            return result
        def estimator(details, now):
            sid = details['listing_id']; car = by_id[sid]; valuation_calls[sid] += 1
            if scenario == 'recovery' and car['index'] % 29 == 0 and valuation_calls[sid] == 1:
                raise TimeoutError('synthetic_valuation_timeout')
            outcome = estimate(details, peers, now)
            latest_outcomes[sid] = outcome
            return outcome.as_dict()
        def drain():
            p.process_details(loader, clock)
            p.process_valuations(estimator, clock)
        for uid in range(1, users + 1):
            for sid in (uid * 2, uid * 2 + 1):
                p.put_recipient(uid, sid, _filters(uid), started_at=NOW - 1000,
                                access_until=NOW - 1 if scenario == 'recovery' and uid % 20 == 1 else NOW + 10000,
                                enabled=not (scenario == 'recovery' and uid % 20 == 0),
                                access_known=not (scenario == 'recovery' and uid % 20 == 2))
        p.start_scan('cold', clock, NOW - 3600, page_budget=20)
        initial = fixture['targets'][:-1] if scenario == 'recovery' else fixture['targets']
        pages = [initial[i:i+100] for i in range(0, len(initial), 100)]
        observed = Counter(); cursor_preserved = True
        for number, page in enumerate(pages, 1):
            html = _feed_html(page)
            # Exercise the source retry/backoff path separately from transport callbacks.
            if scenario == 'recovery' and number == 2:
                p.record_scan_failure('cold', 'rate_limited', clock, retry_after=7)
                observed['source_parse_attempts'] += 1
                denied = p.ingest_html_page('cold', number, html, has_more=number < len(pages), now=clock)
                observed['source_backoff_observed'] += denied['reason'] == 'source_backoff'
                clock += 7
            for _ in range(10):
                before = next(s for s in p.rows('scans') if s['id'] == 'cold')['next_page']
                observed['source_parse_attempts'] += 1
                outcome = p.ingest_html_page('cold', number, html, has_more=number < len(pages), now=clock)
                if outcome['accepted']:
                    break
                if outcome['reason'] != 'queue_capacity':
                    raise AssertionError(outcome['reason'])
                observed['overflow_events'] += 1
                after = next(s for s in p.rows('scans') if s['id'] == 'cold')['next_page']
                cursor_preserved &= before == after == number
                clock += 10; drain()
            else:
                raise AssertionError('fixture_did_not_drain')
            if number == 1:
                p.close(); p = DurablePipeline(path, **policy); p.recover(clock + 1)
                clock += 1; observed['restarts'] += 1
        if scenario == 'recovery':
            p.start_scan('late', clock, NOW - 3600, page_budget=2)
            # A previously seen high ID plus a late lower ID tests overlap + dedupe.
            for _ in range(10):
                observed['source_parse_attempts'] += 1
                late = p.ingest_html_page('late', 1, _feed_html([fixture['targets'][0], fixture['targets'][-1]]),
                                          has_more=False, now=clock)
                if late['accepted']:
                    observed['late_low_id_intake'] += 1
                    break
                if late['reason'] != 'queue_capacity':
                    raise AssertionError(late['reason'])
                observed['overflow_events'] += 1
                clock += 10; drain()
            else:
                raise AssertionError('late_fixture_did_not_drain')
        for _ in range(5):
            clock += 20; drain()
        # Queue-time to send-time races. Manual access must not undo /stop.
        if scenario == 'recovery':
            for uid in range(1, users + 1):
                if uid % 20 in (0, 3):
                    p.stop_user(uid); p.set_access(uid, clock + 10000)
                elif uid % 20 == 4:
                    p.set_access(uid, clock - 1)
        sent = set(); send_calls = Counter(); sender_events = Counter()
        duplicate_acceptances = 0
        def sender(claim, details):
            nonlocal duplicate_acceptances
            pair = (claim['listing_id'], claim['user_id']); send_calls[pair] += 1
            car = by_id[claim['listing_id']]
            code = (car['index'] * 31 + claim['user_id']) % 97
            outcome = 'accepted'
            if scenario == 'recovery':
                if code == 0 and claim['method'] == 'photo':
                    outcome = 'photo_invalid'
                elif code == 1 and send_calls[pair] == 1:
                    outcome = 'rate_limited'
                elif code == 2:
                    outcome = 'uncertain'
                elif code == 3 and send_calls[pair] <= 2:
                    outcome = 'known_transient'
                elif code == 4:
                    outcome = 'blocked'
            sender_events[outcome] += 1
            if outcome == 'accepted':
                if pair in sent:
                    duplicate_acceptances += 1
                    raise AssertionError('duplicate_fixture_acceptance')
                sent.add(pair)
                return dict(outcome=outcome, message_id=claim['user_id'])
            return dict(outcome=outcome, retry_after=10)
        p.send_due(sender, clock)
        if scenario == 'recovery':
            # Simulated manual approval makes previously unknown access known,
            # using the already durable pending claims; it is no real approval.
            for uid in range(1, users + 1):
                if uid % 20 == 2:
                    p.set_access(uid, clock + 10000)
                    observed['manual_access_resolved'] += 1
        for _ in range(3):
            clock += 20; p.send_due(sender, clock)
        summary = p.summary(clock); listings = p.rows('listings'); claims = p.rows('claims')
        # Sender exceptions deliberately become uncertain in the pipeline, so
        # duplicate assertions must also be checked outside that exception boundary.
        if duplicate_acceptances:
            raise AssertionError('duplicate_fixture_acceptance')
        baseline_counts, baseline_sent = _baseline_model(fixture, users, peers)
        truth = {(c['listing_id'], uid) for c in fixture['targets'] for uid in range(1, users + 1)
                 if _truth_pair(c, uid, scenario)}
        labels = [dict(outcome=latest_outcomes[c['listing_id']], should_qualify=c['should_qualify'])
                  for c in fixture['targets'] if c['listing_id'] in latest_outcomes]
        timeline = {r['id']:r for r in listings}
        unknown_pairs = {(r['listing_id'], r['user_id']) for r in claims if r['state'] in ('uncertain','unresolved','pending')}
        accepted = [r for r in claims if r['state'] == 'sent']
        report = dict(
            scenario=scenario, synthetic=True, fixture_cars=cars, users=users, searches_per_user=2,
            peer_corpus=dict(rows=len(peers), created_from_fixture_html_this_run=True, initial_paid_cache_rows=0,
                             actual_estimator='experimental_peer_asking_v1', labels='independent_declared_latent_fixture_market'),
            stages=dict(distinct_source_pages_committed=len(pages) + (1 if scenario == 'recovery' else 0),
                        source_page_parse_attempts=observed['source_parse_attempts'],
                        distinct_discovered=len(listings), detail_attempts=sum(parse_calls.values()),
                        distinct_details_parsed=sum(bool(r['details']) for r in listings),
                        valuation_attempts=sum(valuation_calls.values()), valuation_quality=evaluate_labeled_cases(labels),
                        listing_stages=summary['listings'], pairs=summary['claims'],
                        unresolved_listing_reasons=dict(Counter(r['reason'] for r in listings if r['stage']=='unresolved')),
                        oldest_unprocessed_age_seconds=summary['oldest_unprocessed_age_seconds']),
            delivery=dict(truth_eligible_pairs=len(truth), accepted_pairs=len(sent),
                          true_positive_pairs=len(sent & truth), false_positive_pairs=len(sent - truth),
                          missed_truth_pairs_including_unknown=len(truth - sent),
                          explicitly_unknown_delivery_pairs=len(unknown_pairs),
                          unknown_delivery_true_pairs=len(unknown_pairs & truth),
                          unique_cars_with_acceptance=len({sid for sid,_ in sent}),
                          duplicate_acceptances=duplicate_acceptances, sender_outcomes=dict(sender_events),
                          ambiguous_pairs_with_more_than_one_attempt=sum(send_calls[(r['listing_id'],r['user_id'])]>1 for r in claims if r['state']=='uncertain')),
            recovery=dict(observed, cursor_preserved_on_overflow=cursor_preserved,
                          stop_then_manual_grant_remained_stopped=all(not r['enabled'] for r in p.rows('recipients') if scenario=='recovery' and r['user_id']%20 in (0,3))),
            baseline_policy_model=dict(label='restricted_counterfactual_not_current_production_execution',
                policies=['two_pages_of_100','eager_preview_filter','one_shot_transient_handling'],
                same_targets_and_current_details_and_estimator=True, counts=dict(baseline_counts),
                accepted_truth_pairs=len(baseline_sent & truth), false_positive_pairs=len(baseline_sent-truth),
                missed_truth_pairs=len(truth-baseline_sent)),
            latency_synthetic_seconds=dict(
                publication_to_discovery=_stats([r['discovered']-r['published'] for r in listings if r['published'] is not None]),
                discovery_to_estimate=_stats([r['valued']-r['discovered'] for r in listings if r['valued'] is not None]),
                estimate_to_accepted=_stats([r['accepted']-timeline[r['listing_id']]['valued'] for r in accepted]),
                publication_to_accepted=_stats([r['accepted']-timeline[r['listing_id']]['published'] for r in accepted])),
            outbound=dict(paid_calls=0, successful_external_calls=0, guard=summary['outbound_guard'], outer_guard=outer_guard.summary()),
            source_coverage_proven=False, production_ready=False,
            limitations=['synthetic_clock_not_real_Telegram_or_source_latency',
                         'SQLite_single_process_not_production_throughput',
                         'truth_labels_are_synthetic_not_verified_sales_or_AUTO_RIA_appraisals',
                         'peer_price_bias_and_borderline_false_negatives_are_deliberate',
                         'photo_and_price_kind_annotations_are_synthetic_not_live_parser_claims',
                         'counterfactual_is_not_a_claim_about_all_current_production_policies'])
        p.close()
    report['wall_clock_runtime_seconds'] = round(time.perf_counter()-started, 6)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users', type=int, default=None,
                        help='One cohort size; default runs both 100 and 200 users.')
    parser.add_argument('--cars', type=int, default=240)
    parser.add_argument('--scenario', choices=('normal','recovery','both'), default='both')
    parser.add_argument('--output', type=Path,
                        help='Write the complete synthetic report as JSON to this file.')
    args = parser.parse_args()
    cohorts = (100, 200) if args.users is None else (args.users,)
    scenarios = ('normal', 'recovery') if args.scenario == 'both' else (args.scenario,)
    reports = [run_benchmark(users=u, cars=args.cars, scenario=s) for u in cohorts for s in scenarios]
    result = reports if len(reports) > 1 else reports[0]
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(serialized + '\n', encoding='utf-8')
        print(json.dumps({'output': str(args.output), 'runs': len(reports),
                          'paid_calls': 0, 'successful_external_calls': 0}))
    else:
        print(serialized)


if __name__ == '__main__':
    main()
