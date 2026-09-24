import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import notification_diagnostic as diagnostic
from backend.models import Delivery, Filters, Listing, MonitorJob, SourceBudget, SourceCache, SourceProbe
from backend.tests.test_ria_search import engine, fixture_fetch, raw


def test_once_only_accounted_and_no_notification_side_effects(engine, caplog):
    calls = []

    def fetch(key, path, params):
        calls.append(path)
        return raw("40034669", VIN="private-vin", description="private-seller-text",
                   technicalCondition={}, autoInfoBar={"damage": False})

    diagnostic.check_once(engine, "private-api-key", "40034669", fetch)
    diagnostic.check_once(engine, "private-api-key", "40034669", fetch)
    assert calls == ["info"]
    with Session(engine) as db:
        probe = db.get(SourceProbe, "notification-diagnostic-v1-40034669")
        assert probe.requests == 1 and probe.status == "checked"
        assert probe.result["valuation_blockers"] == ["unverified_condition"]
        assert probe.result["monitor_job"] is None
        assert probe.result["condition_fields"]["technical_id"]["value"] is None
        assert db.get(SourceBudget, "auto_ria").total == 3
        for model in (Delivery, Listing, MonitorJob):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        cached = db.scalar(select(SourceCache))
        assert cached.payload["id"] == "40034669"
        assert "condition_fields" not in cached.payload
    assert "Notification diagnostic" in caplog.text
    for secret in ("private-api-key", "private-vin", "private-seller-text"):
        assert secret not in caplog.text


def test_respects_existing_quota_and_does_not_reset_it(engine):
    with Session(engine) as db:
        row = db.get(SourceBudget, "auto_ria")
        row.blocked_until = time.time() + 60
        db.commit()
    def forbidden(*args):
        pytest.fail("Blocked quota must not fetch")
    diagnostic.check_once(engine, "key", "40034669", forbidden)
    with Session(engine) as db:
        probe = db.get(SourceProbe, "notification-diagnostic-v1-40034669")
        assert probe.result["error"] == "quota_exceeded"
        assert probe.requests == 0
        assert db.get(SourceBudget, "auto_ria").total == 2


def test_filter_dictionaries_share_three_request_cap(engine, monkeypatch):
    filters = Filters(brand="Volkswagen", model="Golf", region="Хмельницька область")
    monkeypatch.setattr(diagnostic, "active_members", lambda db: [
        (SimpleNamespace(filters=filters.canonical()), None, SimpleNamespace(started_at=1))])
    calls = []
    diagnostic.check_once(engine, "key", "123", fixture_fetch(calls))
    with Session(engine) as db:
        probe = db.get(SourceProbe, "notification-diagnostic-v1-123")
        assert probe.requests == len(calls) == 3
        assert probe.result["error"] == "search_limit"
        assert probe.result["subscriptions"]["checked"] == 0
        assert db.get(SourceBudget, "auto_ria").total == 5


def test_date_comparison_does_not_spend_calls_on_undocumented_id_filter(engine, monkeypatch):
    monkeypatch.setattr(diagnostic, "active_members", lambda db: [
        (SimpleNamespace(filters=Filters().canonical()), None, SimpleNamespace(started_at=1))])
    diagnostic.check_once(engine, "key", "123", lambda *args: raw())
    calls = []
    def dates(*args):
        pytest.fail("Undocumented ID filter must not spend provider calls")
    diagnostic.check_dates_once(engine, "key", "123", dates)
    diagnostic.check_dates_once(engine, "key", "123", dates)
    with Session(engine) as db:
        probe = db.get(SourceProbe, "notification-diagnostic-v1-123")
        assert probe.requests == probe.result["requests_used"] == 1
        assert probe.result["date_check"]["status"] == "unverified_id_filter"
        assert probe.result["date_check"]["after"] and probe.result["date_check"]["before"]
        assert db.get(SourceBudget, "auto_ria").total == 3
        assert db.scalar(select(func.count()).select_from(MonitorJob)) == 0
    assert calls == []


def test_unsupported_id_date_result_is_rechecked_with_documented_vin_filter(engine, caplog):
    source_id = "39658048"
    vin = "TMBHS21Z982124886"
    with Session(engine) as db:
        db.add(SourceProbe(id="notification-diagnostic-v1-" + source_id,
            status="checked", checked_at=time.time(), result={"monitor_job": None,
                "subscriptions": {"matching": 1},
                "date_check": {"status": "checked", "created": False,
                               "published": False, "after": "2026-09-23T00:00:00Z",
                               "before": "2026-09-24T00:00:00Z"}}, requests=3))
        db.commit()
    calls = []

    def fetch(key, path, params):
        calls.append((path, params))
        if path == "info":
            return raw(source_id, VIN=vin)
        assert params["VIN[0]"] == vin and "auto_ids[0]" not in params
        ids = [source_id] if "created_after" in params else []
        return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}

    diagnostic.check_vin_dates_once(engine, "private-api-key", source_id, fetch)
    diagnostic.check_vin_dates_once(engine, "private-api-key", source_id, fetch)
    with Session(engine) as db:
        probe = db.get(SourceProbe, "notification-diagnostic-v2-" + source_id)
        assert probe.status == "checked" and probe.requests == 3
        assert probe.result["created"] is True
        assert probe.result["published"] is False
        assert db.get(SourceProbe, "notification-diagnostic-v1-" + source_id).requests == 3
        assert db.scalar(select(func.count()).select_from(Delivery)) == 0
        assert db.scalar(select(func.count()).select_from(MonitorJob)) == 0
        assert db.get(SourceBudget, "auto_ria").total == 5
    assert len(calls) == 3
    for secret in (vin, "private-api-key"):
        assert secret not in caplog.text


def test_vin_date_check_does_not_run_for_observed_or_already_found_listing(engine):
    for source_id, job, created in (("123", {"state": "checked"}, False),
                                    ("124", None, True)):
        with Session(engine) as db:
            db.add(SourceProbe(id="notification-diagnostic-v1-" + source_id,
                status="checked", checked_at=time.time(), result={"monitor_job": job, "subscriptions": {"matching": 1},
                        "date_check": {"status": "checked", "created": created,
                                       "published": False, "after": "2026-09-23T00:00:00Z",
                                       "before": "2026-09-24T00:00:00Z"}}))
            db.commit()
        diagnostic.check_vin_dates_once(engine, "key", source_id,
            lambda *_: pytest.fail("Already explained incident must not spend quota"))
        with Session(engine) as db:
            assert db.get(SourceProbe, "notification-diagnostic-v2-" + source_id) is None


@pytest.mark.parametrize("value", ["../info", "12?api_key=x", "0", "１", "1" * 13])
def test_diagnostic_id_cannot_change_request_path(value):
    with pytest.raises(ValueError):
        diagnostic.validate_id(value)
