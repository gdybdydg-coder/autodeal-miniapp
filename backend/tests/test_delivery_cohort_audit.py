import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import delivery_cohort_audit as audit
from backend.models import Delivery, DeliveryTiming, MonitorFeed, MonitorMembership, Range
from backend.tests.test_monitor import p, add_search, drain, wake


def row(uid, queued, started, accepted, evaluated=None):
    return {"delivery_id": uid, "recipient": uid, "source_id": "123",
            "queued_at": queued, "send_started_at": started,
            "accepted_at": accepted, "evaluated_at": evaluated}


@pytest.mark.parametrize("rows,seen,stage", [
    ([row(1, 100, 101, 102), row(2, 280, 281, 282)], {(1, "123"): 90, (2, "123"): 270}, "later_search_observation"),
    ([row(1, 100, 101, 102), row(2, 280, 281, 282)], {}, "before_enqueue"),
    ([row(1, 100, 101, 102), row(2, 100, 281, 282)], {}, "after_enqueue"),
    ([row(1, 100, 101, 102), row(2, 100, 101, 282)], {}, "telegram_request"),
    ([row(1, 100, 101, 102), row(2, 150, 220, 282)], {}, "mixed"),
    ([row(1, 100, 101, 102), row(2, 100, 102, 103)], {}, "under_30_seconds"),
    ([row(1, 100, None, 102), row(2, 100, 281, 282)], {}, "insufficient_timing"),
])
def test_stages_and_signed_decomposition(rows, seen, stage):
    result = audit.summarize(rows, seen)
    assert result["dominant_observed_stage"] == stage
    if stage != "insufficient_timing":
        assert result["acceptance_gap_seconds"] == pytest.approx(sum(result[k] for k in
            ["queue_entry_delta_seconds", "send_wait_delta_seconds", "request_delta_seconds"]))
    assert all(key not in json.dumps(result) for key in ['"recipient"', '"delivery_id"', '"user_id"'])


def test_invalid_current_seen_record_is_not_claimed_as_historical_discovery():
    result = audit.summarize([row(1, 100, 101, 102), row(2, 280, 281, 282)],
                            {(1, "123"): 90, (2, "123"): 999})
    assert result["last_recipient"]["seen_at"] is None
    assert result["dominant_observed_stage"] == "before_enqueue"


def test_different_search_schedules_reproduce_three_minute_gap_without_sender_wait(p):
    add_search(p, price=Range(to=20000))
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    with Session(p.engine) as db:
        later = db.get(MonitorMembership, 2)
        db.get(MonitorFeed, later.feed_id).next_poll = p.clock[0] + 180
        db.commit()
    drain(p)
    assert [uid for uid, car in p.sent if car.source_id == "124"] == [111]
    p.clock[0] += 180
    drain(p)
    assert [uid for uid, car in p.sent if car.source_id == "124"] == [111, 222]
    # The later search safely refreshes expired details; the time gap already
    # exists before enqueue and is not caused by the independent sender.
    assert len([params for path, params in p.calls if path == "info" and params["auto_id"] == "124"]) == 2
    writes = []
    def capture(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(sql)
    before = len(p.calls), len(p.sent)
    event.listen(p.engine, "before_cursor_execute", capture)
    try:
        with Session(p.engine) as db:
            result = audit.snapshot(db, p.clock[0])
        gap = result["largest_gaps"][0]
        assert gap["dominant_observed_stage"] == "later_search_observation"
        assert 179 <= gap["acceptance_gap_seconds"] <= 181
        assert gap["queue_entry_delta_seconds"] == pytest.approx(180, abs=1)
        assert gap["search_observation_delta_seconds"] == pytest.approx(180, abs=1)
        assert gap["send_wait_delta_seconds"] == pytest.approx(0, abs=1)
        assert result["sample"]["cohorts"] == 1
    finally:
        event.remove(p.engine, "before_cursor_execute", capture)
    assert not writes and (len(p.calls), len(p.sent)) == before


def test_uncertain_receipt_not_compared_and_record_limit_omits_incomplete_cohort(p, monkeypatch):
    add_search(p)
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        monkeypatch.setattr(audit, "MAX_RECORDS", 1)
        result = audit.snapshot(db, p.clock[0])
        assert result["records_truncated"] and result["sample"]["cohorts"] == 0
        monkeypatch.setattr(audit, "MAX_RECORDS", 10000)
        other = db.scalar(select(Delivery).where(Delivery.user_id == 222))
        other.state = "uncertain"
        db.commit()
        assert audit.snapshot(db, p.clock[0])["sample"]["cohorts"] == 0


def test_explicit_opt_in_and_sanitized_failure(p, monkeypatch, caplog):
    calls = []
    def fail(*args):
        calls.append(True)
        raise RuntimeError("secret-must-not-leak")
    monkeypatch.setattr(audit, "snapshot", fail)
    monkeypatch.delenv("RIA_DELIVERY_COHORT_AUDIT", raising=False)
    audit.log_once(p.engine)
    assert not calls
    monkeypatch.setenv("RIA_DELIVERY_COHORT_AUDIT", "true")
    audit.log_once(p.engine)
    assert calls and "RuntimeError" in caplog.text
    assert "secret-must-not-leak" not in caplog.text
