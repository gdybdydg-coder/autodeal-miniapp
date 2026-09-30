from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import notification_diagnostic as diagnostic
from backend.models import Delivery, MonitorJob, SourceBudget, SourceProbe
from backend.tests.test_monitor import p, drain
from backend.tests.test_ria_search import raw


def test_busy_startup_diagnostic_retries_unclaimed_stages_once_without_alerts(p):
    drain(p)
    p.runner.settings = replace(p.settings, ria_diagnostic_listing_id="77")
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.owner, budget.busy_until = "catalog", p.clock[0] + 10
        initial = budget.total
        db.commit()
    calls = []
    def fetch(key, path, params):
        calls.append(path)
        if path == "info":
            value = raw("77", VIN="TMBHS21Z982124886")
            value["autoData"]["fuelName"] = "Дизель, 2 л."
            return value
        ids = ["77"] if "created_after" not in params and "published_after" not in params else []
        return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
    assert diagnostic.retry_selected(p.runner, fetch)
    assert calls == [] and diagnostic.needs_retry(p.engine, "77")
    p.clock[0] += 16
    assert diagnostic.retry_selected(p.runner, fetch)
    assert calls == ["info", "info", "search", "search", "info", "search"]
    assert not diagnostic.retry_selected(p.runner, fetch)
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == initial + 6
        assert db.get(SourceProbe, "notification-diagnostic-v2-77").result["published"] is False
        assert db.get(SourceProbe, "notification-diagnostic-v3-77").result["found"] is True
        assert db.scalar(select(MonitorJob)) is None and db.scalar(select(Delivery)) is None


def test_busy_diagnostic_retries_are_bounded_without_resetting_claimed_probes(p):
    drain(p)
    p.runner.settings = replace(p.settings, ria_diagnostic_listing_id="77")
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        budget.owner, budget.busy_until = "catalog", p.clock[0] + 1000
        initial = budget.total
        db.commit()
    for _ in range(6):
        assert diagnostic.retry_selected(p.runner, lambda *_: None)
        p.clock[0] += 16
    assert not diagnostic.retry_selected(p.runner, lambda *_: None)
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == initial
        db.add(SourceProbe(id="notification-diagnostic-v1-77", status="checking",
                           checked_at=p.clock[0], requests=1, result={}))
        db.commit()
    assert not diagnostic.needs_retry(p.engine, "77")
