"""Evidence-only nearest-rank latency summaries; empty measurements stay unknown."""
import math


def summary(values):
    samples=list(values)
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in samples):
        raise ValueError('invalid_latency_measurement')
    samples.sort();n=len(samples)
    return {'n':n,'p50':samples[math.ceil(n*.5)-1] if n else None,
            'p95':samples[math.ceil(n*.95)-1] if n else None,
            'max':samples[-1] if n else None,'unit':'seconds','method':'nearest_rank'}


def queue_report(queue):
    return {'publication_to_discovery':summary([]),
            'publication_time_proven':False,'coverage':'persisted_candidates_only',
            'stage_intervals_wall_clock':{name:summary(values) for name,values in queue.durations().items()},
            'note':'Wall-clock intervals include queue/restart delays; not monotonic HTTP durations.'}
