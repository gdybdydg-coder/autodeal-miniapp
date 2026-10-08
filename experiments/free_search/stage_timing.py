"""Evidence-only stage durations; no polling, networking or delivery behavior.

All evidence is caller supplied. Publication and Telegram timestamps need their
explicit provenance; neither page download nor local rendering substitutes.
A completed unknown assessment is timed separately from a usable quote.
"""
import math

STAGES = frozenset({'publication', 'source_observed', 'collector_detected',
                    'details_ready', 'assessment_ready', 'message_prepared',
                    'queue_entered', 'queue_released', 'telegram_delivered'})
PROOFS = {
    'publication': {'verified_publication'},
    'source_observed': {'public_snapshot'},
    'collector_detected': {'collector_scan'},
    'details_ready': {'public_details_parsed'},
    'assessment_ready': {'local_assessment'},
    'message_prepared': {'local_render'},
    'queue_entered': {'queue_receipt'},
    'queue_released': {'queue_receipt'},
    'telegram_delivered': {'telegram_delivery_receipt'},
}


def stage_timing(events, *, environment, assessment_status):
    if environment not in {'local', 'render'} or assessment_status not in {'estimated', 'unknown', 'error', 'not_attempted'}:
        raise ValueError('invalid_timing_context')
    stamps = dict.fromkeys(sorted(STAGES))
    rejected = {}
    for name, event in events.items():
        if name not in STAGES or not isinstance(event, dict):
            raise ValueError('invalid_stage')
        at = event.get('at')
        if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
            raise ValueError('invalid_event_time')
        if event.get('proof') not in PROOFS[name]:
            rejected[name] = 'unsupported_provenance'
        else:
            stamps[name] = at
    if assessment_status == 'not_attempted' and stamps['assessment_ready'] is not None:
        raise ValueError('assessment_status_conflict')
    def duration(start, end):
        a, b = stamps[start], stamps[end]
        if a is None or b is None:
            return None
        if b < a:
            raise ValueError('event_order_conflict')
        return b - a
    durations = {
        'publication_to_detection_seconds': duration('publication', 'collector_detected'),
        'detection_to_details_seconds': duration('collector_detected', 'details_ready'),
        'details_to_assessment_seconds': duration('details_ready', 'assessment_ready'),
        'detection_to_assessment_seconds': duration('collector_detected', 'assessment_ready'),
        'publication_to_message_seconds': duration('publication', 'message_prepared'),
        'queue_wait_seconds': duration('queue_entered', 'queue_released'),
        'queue_release_to_delivery_seconds': duration('queue_released', 'telegram_delivered'),
    }
    return {'environment': environment, 'assessment_status': assessment_status,
            'events': stamps, 'rejected_evidence': rejected, 'durations': durations,
            'first_source_availability_at': None,
            'source_observed_is_first_availability': False,
            'market_price_verified': False, 'caller_evidence_independently_verified': False,
            'production_approved': False}
