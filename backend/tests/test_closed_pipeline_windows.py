"""Recorded event windows, not synthetic proof of production delivery."""
from sqlalchemy import event
from sqlalchemy.orm import Session

from backend import source_pipeline_health as health
from backend.api_attempt_audit import RiaApiAttempt
from backend.models import Delivery, DeliveryTiming, Listing, MonitorJob, SourceProbe
from backend.tests.test_monitor import p


def test_closed_windows_use_event_time_and_do_not_double_count_or_write(p):
    end = int(p.clock[0] // 3600) * 3600
    with Session(p.engine) as db:
        for i, at in enumerate((end-86401, end-86400, end-3600, end-1, end)):
            db.add(RiaApiAttempt(id=str(i), request_fingerprint=str(i), category="search",
                reserved_at=at-2, transport_started_at=at, state="success",
                call_relation="first_observed", request_ordinal=1, forced=False))
            db.add(MonitorJob(source_id=str(i), state="unvalued", first_seen=at,
                result={"candidate": {"id": str(i)}, "evaluated_at": at,
                        "rating": {"valuation": "unavailable"}}))
        for i, at in enumerate((end-3600, end-1, end, end-10), start=900):
            db.add(Listing(id=i, source="auto_ria", source_id="window-"+str(i), car={}))
            db.add(Delivery(id=i, listing_id=i, user_id=111, state="sent", message_id=i))
            db.add(DeliveryTiming(delivery_id=i, queued_at=at-120,
                send_started_at=at-1, accepted_at=at))
        db.add(SourceProbe(id="owner-car-copy-v1-903", status="sent", checked_at=end, result={}))
        db.commit()
    def readonly(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT"), statement
    event.listen(p.engine, "before_cursor_execute", readonly)
    try:
        with Session(p.engine) as db:
            out = health.closed_windows_snapshot(db, p.clock[0])
            assert not db.new and not db.dirty and not db.deleted
    finally:
        event.remove(p.engine, "before_cursor_execute", readonly)
    hour, day = out["last_complete_hour"], out["preceding_24_hours"]
    assert hour["window"] == {"after_inclusive": end-3600, "before_exclusive": end}
    assert hour["source_attempts"]["transport_by_category"] == {"search": 2}
    assert day["source_attempts"]["transport_by_category"] == {"search": 3}
    assert hour["candidate_ids_first_seen"] == 2
    assert day["candidate_ids_first_seen"] == 3
    assert hour["evaluated_ids_by_saved_result"] == {"unavailable": 2}
    assert hour["ordinary_delivery_events"]["accepted"] == 2
    assert hour["ordinary_delivery_events"]["queued"] == 2
    assert hour["ordinary_delivery_events"]["distinct_accepted_recipients"] == 1
    assert hour["matching_recipients_at_evaluation"] is None
    assert not p.calls and not p.sent
