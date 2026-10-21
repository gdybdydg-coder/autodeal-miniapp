"""Wall-clock evidence stages, never publication or Telegram latency.

Browser and collector timestamps share a local clock. Includes UI/tool waiting.
Reject clock regression rather than silently publishing a negative duration.
"""
import math


def handoff_timings(first_seen,detail_received,captured,qualified):
    values=(first_seen,detail_received,captured,qualified)
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in values):
        raise ValueError('invalid_stage_clock')
    if not first_seen<=detail_received<=captured<=qualified:
        raise ValueError('stage_clock_regression')
    return {'unit':'seconds','clock':'wall_clock_includes_tool_wait',
            'first_observed_to_detail':detail_received-first_seen,
            'detail_to_browser_capture':captured-detail_received,
            'browser_capture_to_qualification':qualified-captured,
            'first_observed_to_qualification':qualified-first_seen,
            'publication_to_discovery':None,'telegram_acceptance':None}
