import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import launch
from backend.models import (Delivery, DeliveryTiming, MonitorJob, MonitorSeen, Search,
                            SourceBudget, SourceProbe)
from backend.ria_search import parse_car
from backend.tests.test_backend import setup, headers, subscribe
from backend.tests.test_monitor import p, drain, wake
from backend.tests.test_ria_search import engine, raw


def test_source_timestamp_requires_an_explicit_timezone_and_real_past_time():
    value = "2026-01-01T12:30:00+02:00"
    expected = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc).timestamp()
    assert launch.source_added_at(value) == expected
    assert parse_car(raw(addDate=value), "123")["source_added_at"] == expected
    assert launch.source_added_at("2026-01-01T10:30:00Z") == expected
    for invalid in (None, "", "2026-01-01 12:30:00", "2026-01-01", "2100-01-01T00:00:00Z", "invalid"):
        assert launch.source_added_at(invalid) is None


def test_launch_accounting_starts_only_when_enabled_and_never_resets(engine):
    launch.initialize(engine, False)
    assert launch.status(engine, False)["started_at"] is None
    launch.initialize(engine, True)
    started = launch.status(engine, True)
    assert started["provider_requests_since_start"] == 0
    with Session(engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.total += 7
        db.commit()
    launch.initialize(engine, True)
    resumed = launch.status(engine, True)
    assert resumed["started_at"] == started["started_at"]
    assert resumed["provider_requests_since_start"] == 7
    assert resumed["activity"]["last_delivery"] is None
    assert resumed["activity"]["messages_accepted"] == 0


def test_pipeline_records_real_server_stages_and_unknown_publication_time(p):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    discovered_at = p.clock[0]
    p.runner.tick()
    p.clock[0] += 3
    p.runner.tick()
    p.clock[0] += 2
    def sender(uid, car):
        p.sent.append((uid, car))
        p.clock[0] += 1.5
        return {"ok": True, "result": {"message_id": 8, "date": int(p.clock[0])}}
    p.runner.sender = sender
    p.runner.deliver_tick()
    assert len(p.sent) == 1
    with Session(p.engine) as db:
        timing = db.scalar(select(DeliveryTiming))
        assert timing.discovered_at == discovered_at
        assert timing.evaluated_at == discovered_at + 3
        assert timing.send_started_at == discovered_at + 5
        assert timing.accepted_at == discovered_at + 6.5
        assert timing.telegram_date == int(timing.accepted_at)
        own, other = launch.activity(db, 111), launch.activity(db, 222)
        assert own["messages_accepted"] == own["new_listings"] == own["evaluated"] == 1
        assert own["last_delivery"]["discovery_to_telegram_seconds"] == 6.5
        assert own["last_delivery"]["send_request_seconds"] == 1.5
        assert own["last_delivery"]["source_added_to_telegram_seconds"] is None
        assert other["messages_accepted"] == other["new_listings"] == other["evaluated"] == 0
        assert other["last_delivery"] is None
    p.runner.deliver_tick()
    assert len(p.sent) == 1  # Observability does not create another delivery.


@pytest.mark.parametrize("result", [{"uncertain": True}, {"error_code": 429, "parameters": {"retry_after": 60}}, {"error_code": 403}])
def test_timeout_rate_limit_and_blocked_bot_are_not_successful_deliveries(p, result):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    p.runner.tick()
    p.runner.tick()
    p.runner.sender = lambda *_: result
    p.runner.deliver_tick()
    with Session(p.engine) as db:
        timing = db.scalar(select(DeliveryTiming))
        assert timing.send_started_at and timing.accepted_at is None
        assert launch.activity(db, 111)["messages_accepted"] == 0
        assert launch.activity(db, 111)["last_delivery"] is None


def test_authenticated_progress_is_owner_scoped_and_reads_do_not_spend_requests(setup):
    engine, settings, client = setup
    first = subscribe(client, enabled=False, uid=111).json()["id"]
    second = subscribe(client, enabled=False, uid=222).json()["id"]
    now = time.time()
    with Session(engine) as db:
        for sid, source, state in [(first, "123", "checked"), (second, "124", "unvalued")]:
            db.add(MonitorSeen(search_id=sid, source_id=source, epoch="test", state=state, first_seen=now))
            db.add(MonitorJob(source_id=source, state=state, first_seen=now,
                result={"rating": {"valuation": "sample_median" if state == "checked" else "insufficient_data",
                                   "valuation_reasons": ["missing_modification_id", "private-unexpected-value"],
                                   "comparables": 0}}))
        before = db.get(SourceBudget, "auto_ria").total
        db.commit()
    assert client.get("/api/notifications/status").status_code == 401
    one = client.get("/api/notifications/status", headers=headers(111)).json()["activity"]
    two = client.get("/api/notifications/status", headers=headers(222)).json()["activity"]
    assert one["new_listings"] == two["new_listings"] == 1
    assert one["evaluated"] == 1 and one["unknown"] == 0
    assert two["evaluated"] == 0 and two["unknown"] == 1
    assert one["latest_unknown_reason"] is None
    assert one["unknown_breakdown"] == {"reasons": {}, "peer_rejections": {}, "comparable_counts": {}}
    assert two["unknown_breakdown"] == {"reasons": {"missing_modification_id": 1},
                                        "peer_rejections": {}, "comparable_counts": {"0": 1}}
    assert two["latest_unknown_reason"] == {"category": "missing_details", "codes": ["missing_modification_id"], "comparables": 0}
    public = client.get("/api/source-status").json()["launch"]
    assert public["readiness"]["saved_subscriptions"] == 2
    assert public["activity"]["new_listings"] == 2
    assert "user_id" not in str(public) and "source_id" not in str(public)
    assert "private-unexpected-value" not in str(public)
    assert public["activity"]["unknown_breakdown"] == two["unknown_breakdown"]
    with Session(engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before
        assert db.scalar(select(Delivery)) is None
