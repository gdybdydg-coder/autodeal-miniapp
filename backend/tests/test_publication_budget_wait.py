"""No production credentials, source HTTP calls or Telegram sends."""
import copy
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import recent_publications as rp
from backend.auto_ria import RiaError
from backend.models import MonitorJob, SourceBudget, SourceProbe, User
from backend.tests.test_monitor import p, details, drain, add_search
from backend.tests.test_recent_publications import card, date, dispatch, fetcher, offer, probe, setup
from backend.tests.test_paid_sources_production import approve, strict


def metadata(sid="77", brand="Volkswagen", model="Golf", year="2012"):
    return (f'<div data-advertisement-data data-id="{sid}" data-mark-name="{brand}" '
            f'data-model-name="{model}" data-year="{year}"></div>')


def with_metadata(body, value):
    return body.replace('</section>', value + '</section>')


def test_real_card_attribute_shape_extracts_only_same_listing_preview():
    at = 1790788800
    row = rp.parse(with_metadata(card("77", at), metadata()))[0]
    assert row["preview"] == {"brand": "Volkswagen", "model": "Golf", "year": 2012}
    for attrs in (metadata(sid="78"), metadata(year="today"), metadata() + metadata(brand="BMW")):
        preview = rp.parse(with_metadata(card("77", at), attrs))[0].get("preview", {})
        assert "year" not in preview or preview["year"] == 2012
        if attrs != metadata(year="today"):
            assert not preview


@pytest.mark.parametrize("preview", [{"brand": "BMW"}, {"model": "Passat"}, {"year": 1990}, {"year": 2030}])
def test_known_preview_contradictions_do_not_spend_detail_or_quote(p, monkeypatch, preview):
    p.filters = p.filters.model_copy(update={"year": rp.Filters.model_validate({"year": {"from": 2000, "to": 2020}}).year})
    with Session(p.engine) as db:
        from backend.models import Search
        search = db.get(Search, 1)
        search.filters = p.filters.canonical()
        db.commit()
    quotes = setup(p, monkeypatch)
    offer(p)
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = copy.deepcopy(row.result); data["pending"]["77"]["preview"] = preview
        row.result = data; db.commit()
    drain(p)
    assert not details(p, "77") and not quotes and not p.sent


def test_unknown_or_matching_preview_still_reaches_official_validation(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    offer(p)
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = copy.deepcopy(row.result); data["pending"]["77"]["preview"] = {"brand": "volkswagen", "model": " GOLF "}
        row.result = data; db.commit()
    drain(p)
    assert len(details(p, "77")) == 1 and quotes == ["77"] and len(p.sent) == 1


@pytest.mark.parametrize("cap", ["hourly", "daily"])
def test_quota_wait_survives_repeated_ticks_restart_and_old_attempt_counter(p, monkeypatch, cap):
    setup(p, monkeypatch); offer(p)
    p.runner.tick()  # Ordinary due primary search is completed first.
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = copy.deepcopy(row.result)
        data["api"] = [p.clock[0]] * (rp.API_HOURLY if cap == "hourly" else rp.API_DAILY)
        data["pending"]["77"]["attempts"] = 3
        row.result = data
        before = db.get(SourceBudget, "auto_ria").total
        db.commit()
    from backend.monitor import Monitor
    for _ in range(4):
        p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
        drain(p)
        p.clock[0] += 61
    data = probe(p)
    assert data["pending"]["77"]["attempts"] == 3
    assert data["pending"]["77"]["wait_reason"] == "intake_" + cap
    assert data["discard_reasons"].get("unavailable_details", 0) == 0
    assert not details(p, "77")
    with Session(p.engine) as db:
        # Only ordinary primary searches may have consumed reservations.
        assert db.get(SourceBudget, "auto_ria").total >= before


def test_short_rolling_hourly_wait_resumes_candidate_and_delivers_once(p, monkeypatch):
    quotes = setup(p, monkeypatch); offer(p); p.runner.tick()
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID); data = dict(row.result)
        data["api"] = [p.clock[0] - 3540] * rp.API_HOURLY
        row.result = data; db.commit()
    drain(p)
    assert not details(p, "77")
    p.clock[0] += 61; drain(p); dispatch(p)
    assert len(details(p, "77")) == 1 and quotes == ["77"] and len(p.sent) == 1


def test_budget_wait_expiry_is_explicit_not_unavailable_details(p, monkeypatch):
    setup(p, monkeypatch); offer(p); p.runner.tick()
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID); data = dict(row.result)
        data["api"] = [p.clock[0]] * rp.API_DAILY; row.result = data; db.commit()
    drain(p); p.clock[0] += rp.MAX_AGE + 1; p.runner.tick()
    rp.collect(p.engine, p.settings, fetcher(card("88", p.clock[0] - 1)))
    data = probe(p)
    assert "77" not in data["pending"]
    assert data["discard_reasons"]["expired"] == 1
    assert data["discard_reasons"].get("unavailable_details", 0) == 0


def test_transient_transport_failures_are_bounded_separately(p, monkeypatch):
    setup(p, monkeypatch); offer(p)
    factory = p.runner.search_factory
    def failed_source(engine, key):
        source = factory(engine, key); original = source.fetch
        def fetch(key, path, params):
            if path == "info" and params["auto_id"] == "77":
                raise RiaError("connection_error")
            return original(key, path, params)
        source.fetch = fetch; return source
    p.runner.search_factory = failed_source
    for _ in range(3):
        drain(p); p.clock[0] += 61
    data = probe(p)
    assert "77" not in data["pending"] and data["discard_reasons"]["unavailable_details"] == 1
    assert len(data["api"]) == 3


def test_public_status_is_read_only_and_separates_primary_budget_delivery(p, monkeypatch):
    strict(p); add_search(p); approve(p, 111); approve(p, 222)
    setup(p, monkeypatch)
    for name, value in (("HOURLY", 900), ("DAILY", 3000), ("TOTAL", 90000)):
        monkeypatch.setenv("RIA_REQUESTS_" + name + "_CAP", str(value))
    with Session(p.engine) as db:
        db.add(User(id=333, ready=True)); db.commit()
        before = db.get(SourceBudget, "auto_ria").total
    result = rp.status(p.engine, p.settings)
    from backend.source_pipeline_health import snapshot
    with Session(p.engine) as db:
        pipeline = snapshot(db, p.settings, p.clock[0])
    assert "pipeline" not in result and "current_paid_clients" not in result
    assert pipeline["current_paid_clients"] == 2
    assert pipeline["ready_enabled_searches"] == 2
    assert pipeline["primary"]["running"] is True
    assert result["api_availability"]["reason"] == "available"
    assert pipeline["delivery"]["receipt_basis"] == "telegram_api_acceptance"
    assert pipeline["api_last_hour"]["provider_charged_units"] is None
    assert "user_id" not in json.dumps(result) and "fixture-111" not in json.dumps(result)
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before


def test_success_clears_stale_collection_error(p, monkeypatch):
    setup(p, monkeypatch)
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID); data = dict(row.result)
        data["last_error"] = "unexpected_html"; row.result = data; db.commit()
    offer(p)
    assert probe(p)["last_error"] is None
