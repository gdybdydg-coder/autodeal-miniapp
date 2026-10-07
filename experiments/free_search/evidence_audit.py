"""Offline evidence accounting; never a collector, sender or production gate.

Unknown publication times stay unknown. Control IDs must come from a separately
defined population; a link census of the collector's page is not recall.
Public probe facts alone are supplied to the existing research estimator.
"""
from dataclasses import dataclass, asdict
import math
from .valuation import estimate


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_timestamp')
    return value


@dataclass(frozen=True)
class Timeline:
    environment: str
    publication_at: float | None = None
    source_first_observed_at: float | None = None
    detected_at: float | None = None
    details_ready_at: float | None = None
    valuation_ready_at: float | None = None
    queued_at: float | None = None
    send_started_at: float | None = None
    telegram_accepted_at: float | None = None

    def report(self):
        if self.environment not in ('local_public_probe', 'render_paid_production', 'synthetic'):
            raise ValueError('invalid_environment')
        times = {k: _timestamp(v) for k, v in asdict(self).items() if k != 'environment'}
        known = [v for v in times.values() if v is not None]
        if known != sorted(known):
            raise ValueError('noncausal_timeline')
        def difference(start, end):
            a, b = times[start], times[end]
            return None if a is None or b is None else round(b-a, 6)
        pairs = {
            'publication_to_source': ('publication_at', 'source_first_observed_at'),
            'source_to_detection': ('source_first_observed_at', 'detected_at'),
            'detection_to_details': ('detected_at', 'details_ready_at'),
            'details_to_valuation': ('details_ready_at', 'valuation_ready_at'),
            'detection_to_valuation': ('detected_at', 'valuation_ready_at'),
            'valuation_to_queue': ('valuation_ready_at', 'queued_at'),
            'queue_wait': ('queued_at', 'send_started_at'),
            'telegram_acceptance': ('send_started_at', 'telegram_accepted_at'),
            'detection_to_acceptance': ('detected_at', 'telegram_accepted_at'),
            'publication_to_acceptance': ('publication_at', 'telegram_accepted_at'),
        }
        return {'environment': self.environment, 'timestamps': times,
                'seconds': {k: difference(*v) for k, v in pairs.items()},
                'phone_push_or_read_proven': False}


def control_overlap(control_ids, observed_ids, *, independent_basis):
    """Bounded ID overlap, deliberately not a whole-market recall metric.

Even a preexisting production log is selected by paid-client filters and has
survivorship bias. Missing IDs require source/age/filter investigation.
"""
    if independent_basis not in ('preexisting_production_log', 'independent_public_sample'):
        raise ValueError('independent_control_required')
    control, observed = set(control_ids), set(observed_ids)
    return {'basis': independent_basis, 'control_count': len(control),
            'found': sorted(control & observed), 'not_observed': sorted(control-observed),
            'sample_overlap_fraction': len(control & observed)/len(control) if control else None,
            'whole_market_recall': None,
            'limitations': ['sample_selection_bias', 'different_observation_times',
                           'absence_is_not_a_proven_source_miss']}


def polling_projection(interval_seconds, page_body_bytes, *, daily_details=0, detail_body_bytes=0):
    """Ideal start-to-start cadence, uniform arrival phase; NOT observed SLA.

Page bodies are observed decoded payloads, not provider-billed wire traffic.
Detail volume and server/storage prices remain explicit inputs/unknowns.
"""
    if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds) or interval_seconds <= 0):
        raise ValueError('invalid_interval')
    if not page_body_bytes or any(type(n) is not int or n < 0 for n in page_body_bytes):
        raise ValueError('invalid_page_sizes')
    if type(daily_details) is not int or daily_details < 0 or type(detail_body_bytes) is not int or detail_body_bytes < 0:
        raise ValueError('invalid_detail_volume')
    cycles = 86400/interval_seconds
    return {'basis': 'hypothetical_24h_uniform_arrival_no_failures_no_cache_delay',
            'interval_seconds': interval_seconds, 'mean_poll_wait_seconds': interval_seconds/2,
            'p95_poll_wait_seconds': interval_seconds*.95,
            'poll_wait_upper_bound_seconds': interval_seconds,
            'feed_requests_per_day': cycles*len(page_body_bytes),
            'detail_requests_per_day': daily_details,
            'decoded_body_gb_per_day': (cycles*sum(page_body_bytes)+daily_details*detail_body_bytes)/1e9,
            'server_cost': None, 'billed_traffic_cost': None, 'storage_cost': None,
            'publication_to_ready_seconds': None, 'production_change_authorized': False}


def public_only_valuation(probe):
    """Use only details inside this fresh probe. No API/cache/label input.

The marker is an input contract, not independent attestation. Network boundary
and probe code review are still needed. No old production snapshots accepted.
"""
    if probe.get('acquisition_basis') != 'fresh_public_html_no_paid_cache':
        raise ValueError('public_provenance_required')
    if probe.get('paid_api_requests') != 0 or probe.get('telegram_requests') != 0:
        raise ValueError('not_a_zero_paid_probe')
    peers = []
    for row in probe['details']:
        detail = row.get('details')
        if not detail or detail.get('availability') != 'active':
            continue
        # Explicit allowlist prevents quotes/labels/hidden input data entering.
        peer = {k: detail.get(k) for k in ('listing_id','brand','model','year','body','fuel',
                                          'transmission','mileage_km','price','currency')}
        visible = row.get('visible') or {}
        if visible.get('listing_id') == peer['listing_id']:
            peer['region'] = visible.get('region')
        peer['observed_at'] = _timestamp(row.get('body_received_at'))
        if peer['observed_at'] is None:
            raise ValueError('detail_observation_time_required')
        peers.append(peer)
    as_of = _timestamp(probe['finished_at'])
    if as_of is None or any(p['observed_at'] > as_of for p in peers):
        raise ValueError('invalid_as_of')
    outcomes = [{'listing_id': p['listing_id'], **estimate(p, peers, as_of).as_dict()} for p in peers]
    return {'basis': 'fresh_public_details_only', 'outcomes': outcomes,
            'parsed_active_subjects': len(peers), 'detail_failures_or_inactive': len(probe['details'])-len(peers),
            'estimated': sum(o['status']=='estimated' for o in outcomes),
            'unknown': sum(o['status']=='unknown' for o in outcomes),
            'production_approved': False, 'production_formula_unchanged': True}
