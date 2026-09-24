from dataclasses import replace

from sqlalchemy.orm import Session

from backend.models import MonitorJob, MonitorSeen, MonitorWatch, Range, SourceProbe, User
from backend.monitor import Monitor, NOTIFICATION_VERSION, reset_watch
from backend.notification_recovery import probe_id, recover_once, report
from backend.tests.test_monitor import p, drain, details, add_search
from backend.ria_search import parse_car
from backend.tests.test_ria_search import raw
from backend.valuation import VERSION


def enable_recovery(p, sid="123"):
    p.runner.settings = replace(p.settings, ria_recovery_listing_id=sid)


def test_reported_old_listing_is_freshly_checked_and_sent_once(p, caplog):
    enable_recovery(p)
    drain(p)
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "123")]
    assert len(details(p, "123")) == 1
    p.runner = Monitor(p.engine, p.runner.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    report(p.engine, "123")
    assert len(p.sent) == 1 and len(details(p, "123")) == 1
    with Session(p.engine) as db:
        probe = db.get(SourceProbe, probe_id("123"))
        assert probe.requests == 0
        assert probe.result["report"]["delivery_states"] == {"sent": 1}
    assert "Notification recovery" in caplog.text
    assert "test-token" not in caplog.text and "test-only" not in caplog.text


def test_recovery_never_bypasses_current_filter_or_discount(p):
    enable_recovery(p)
    add_search(p, sid=2, uid=222, minDiscount=40.)
    add_search(p, sid=3, uid=333, price=Range.model_validate({"from": 0, "to": 5000}))
    drain(p)
    assert [uid for uid, _ in p.sent] == [111]


def test_recovery_does_not_fabricate_market_price_when_peers_are_missing(p):
    enable_recovery(p)
    factory = p.runner.search_factory
    def insufficient(engine, key):
        source = factory(engine, key)
        source.notification_comparisons = lambda _: []
        return source
    p.runner.search_factory = insufficient
    drain(p)
    assert len(p.sent) == 1 and p.sent[0][1].market is None
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "123").state == "informational"
        assert db.get(SourceProbe, probe_id("123")).result["report"]["valuation_reasons"] == ["insufficient_comparables"]


def test_recovery_respects_stop_after_queue_and_does_not_target_later_users(p):
    enable_recovery(p)
    assert p.runner.claim()
    recover_once(p.runner, "123")
    p.runner.release("idle")
    with Session(p.engine) as db:
        db.get(User, 111).ready = False
        reset_watch(db, 1, False)
        db.commit()
    add_search(p, sid=2, uid=222)
    drain(p)
    assert not p.sent and not details(p, "123")
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (2, "123")) is None


def test_recovery_is_disabled_by_default_and_requires_monitor_lease(p):
    recover_once(p.runner, "123")
    drain(p)
    assert not p.sent and not details(p, "123")
    with Session(p.engine) as db:
        assert db.get(SourceProbe, probe_id("123")) is None


def test_old_optional_rejection_is_not_reopened_by_fresh_only_supplement(p):
    p.runner.settings = replace(p.settings, ria_active_window_enabled=True)
    candidate = parse_car(raw("124"), "124")
    candidate["gear_id"] = None
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        db.add(MonitorSeen(search_id=1, source_id="124", epoch=epoch,
                           state="unvalued", first_seen=p.clock[0]))
        db.add(MonitorJob(source_id="124", first_seen=p.clock[0], state="unvalued",
                          result={"candidate": candidate,
                                  "rating": {"valuation_version": VERSION}}))
        db.commit()
    assert p.runner.claim()
    p.runner.sync()
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "unvalued"
        assert db.get(MonitorSeen, (1, "124")).state == "unvalued"
        job = db.get(MonitorJob, "124")
        job.state = "unvalued"
        job.result = {"candidate": candidate, "notification_version": NOTIFICATION_VERSION,
                      "rating": {"valuation_version": VERSION}}
        db.get(MonitorSeen, (1, "124")).state = "unvalued"
        db.commit()
    p.runner.sync()
    p.runner.release("idle")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "unvalued"
