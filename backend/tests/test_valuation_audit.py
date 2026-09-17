import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app import Settings, create_app
from backend.auto_ria import RiaError
from backend.models import Delivery, Listing
from backend.ria_search import parse_car
from backend.ria_validation import comparison_report, validate_once, validation_status
from backend.tests.test_ria_search import engine, configure_caps, fixture_fetch, raw


def popular_fixture(calls, sparse_brand=None):
    makes = {84: ("Volkswagen", "Passat", 5), 6: ("Audi", "A6", 9), 48: ("Mercedes-Benz", "E-Class", 7)}

    def fetch(key, path, params):
        calls.append((path, params))
        if path == "categories/1/marks":
            return [{"name": make[0], "value": code} for code, make in makes.items()]
        if path.endswith("/models"):
            brand = int(path.split("/")[-2])
            return [{"name": makes[brand][1], "value": makes[brand][2]}]
        if path == "search":
            brand = params["marka_id[0]"]
            count = 4 if brand == sparse_brand else 5
            ids = [str(brand * 1000 + i) for i in range(1, count + 1)] if "generation_id[0][0]" in params else [str(brand * 1000)]
            return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
        if path == "info":
            source_id = params["auto_id"]
            brand = int(source_id) // 1000
            name, model, model_id = makes[brand]
            return raw(source_id, USD=10000 if int(source_id) % 1000 == 0 else 15000,
                       markId=brand, modelId=model_id, markName=name, modelName=model,
                       title=name + " " + model, VIN="private-seller-marker", seller={"phone": "private-seller-marker"})
        raise AssertionError(path)

    return fetch


def test_three_models_share_accounting_run_once_and_never_feed_delivery(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls = []
    validate_once(engine, "private-key", "popular-test", popular_fixture(calls), profile="popular-v1")
    result = validation_status(engine, "popular-test", "popular-v1")
    assert result["status"] == "samples_checked"
    assert result["models_planned"] == ["Volkswagen Passat", "Audi A6", "Mercedes-Benz E-Class"]
    assert [query["filters"]["brand"] for query in result["queries"]] == ["Volkswagen", "Audi", "Mercedes-Benz"]
    assert result["valued"] == 3 and result["returned"] == 3
    assert result["requests_used"] == len(calls) == sum(query["requests_used"] for query in result["queries"])
    assert len(calls) <= result["request_cap"] == 96
    for query in result["queries"]:
        assert query["requests_used"] <= 32
        assert query["comparisons"][0]["calculation_matches"]
        assert query["comparisons"][0]["accepted_prices_usd"] == [15000] * 5
        assert query["comparisons"][0]["qualifies_as_deal"]
    count = len(calls)
    validate_once(engine, "private-key", "popular-test", popular_fixture(calls), profile="popular-v1")
    assert len(calls) == count
    assert "private-key" not in str(result) and "private-seller-marker" not in str(result)
    with Session(engine) as db:
        assert db.query(Delivery).count() == 0 and db.query(Listing).count() == 0


def test_each_model_is_bounded_even_when_the_first_cannot_finish(engine, monkeypatch):
    configure_caps(monkeypatch)
    monkeypatch.setattr("backend.ria_validation.MAX_REQUESTS", 4)
    calls = []
    validate_once(engine, "key", "small-cap", popular_fixture(calls), profile="popular-v1")
    result = validation_status(engine, "small-cap", "popular-v1")
    assert result["status"] == "partial" and len(result["queries"]) == 3
    assert len(calls) == result["requests_used"] <= 12
    assert all(query["requests_used"] <= 4 for query in result["queries"])


def test_insufficient_model_does_not_pass_the_whole_audit(engine, monkeypatch):
    configure_caps(monkeypatch)
    validate_once(engine, "key", "sparse", popular_fixture([], sparse_brand=6), profile="popular-v1")
    result = validation_status(engine, "sparse", "popular-v1")
    assert result["status"] == "partial"
    assert result["queries"][1]["status"] == "insufficient_comparables"
    assert result["queries"][1]["comparisons"][0]["deal_threshold_usd"] is None
    assert result["queries"][2]["status"] == "valuation_verified"


def test_upstream_quota_failure_stops_remaining_models(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls = []

    def fetch(*args):
        calls.append(args[1])
        raise RiaError("quota_exceeded")

    validate_once(engine, "key", "blocked", fetch, profile="popular-v1")
    result = validation_status(engine, "blocked", "popular-v1")
    assert len(calls) == result["requests_used"] == 1
    assert len(result["queries"]) == 1 and result["queries"][0]["status"] == "quota_exceeded"
    assert result["status"] == "partial"


def test_shutdown_and_profile_conflict_cannot_trigger_extra_calls(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls, stop = [], threading.Event()
    fixture = popular_fixture(calls)

    def fetch(*args):
        response = fixture(*args)
        stop.set()
        return response

    validate_once(engine, "key", "stopped", fetch, profile="popular-v1", stop=stop)
    assert len(calls) == 1
    result = validation_status(engine, "stopped", "popular-v1")
    assert result["queries"][0]["status"] == "validation_stopped"
    stop.clear()
    validate_once(engine, "key", "stopped", fixture, profile="popular-v1", stop=stop)
    assert len(calls) == 1
    validate_once(engine, "key", "legacy", fixture_fetch([]))
    validate_once(engine, "key", "legacy", fixture, profile="popular-v1")
    assert len(calls) == 1
    assert validation_status(engine, "legacy", "popular-v1")["status"] == "profile_conflict"


def test_explanation_rejects_wrong_peers_and_keeps_the_exact_deal_boundary():
    candidate = parse_car(raw(USD=12750), "123")
    peers = [parse_car(raw(str(i), USD=15000), str(i)) for i in range(124, 129)]
    other = {**peers[0], "id": "999", "modification_id": 999, "year": 2000}
    result = comparison_report(candidate, [candidate, *peers, peers[0], other])
    assert result["duplicate_or_self_entries"] == 2
    assert result["accepted_prices_usd"] == [15000] * 5
    assert result["rejected"] == [{"id": "999", "reasons": ["modification_id", "year"]}]
    assert result["recalculated_market"] == 15000 and result["deal_threshold_usd"] == 12750
    assert result["qualifies_as_deal"] and result["calculation_matches"]
    assert not comparison_report({**candidate, "price_usd": 12750.01}, peers)["qualifies_as_deal"]
    missing = comparison_report({**candidate, "modification_id": None}, peers)
    assert "missing_modification_id" in missing["candidate_reasons"]
    assert missing["recalculated_market"] is None and missing["calculation_matches"]
    mixed = comparison_report(candidate, [*peers[:4], {**peers[4], "price_usd": 40000}])
    assert mixed["mixed_sample"] and mixed["deal_threshold_usd"] is None and mixed["calculation_matches"]


def test_api_starts_while_audit_runs_and_shutdown_signals_it(engine, monkeypatch):
    started, finished = threading.Event(), threading.Event()

    def audit(*args, profile, stop):
        assert profile == "popular-v1"
        started.set()
        assert stop.wait(3)
        finished.set()

    for name in ("probe_once", "verify_search_once", "ria_rollout.check_once", "telegram_setup.configure", "telegram_setup.configure_menu"):
        monkeypatch.setattr("backend.app." + name, lambda *args: None)
    monkeypatch.setattr("backend.app.validate_once", audit)
    settings = Settings(str(engine.url), "fake-token", "x" * 32, auto_ria_api_key="fake-key",
                        ria_validation_run_id="background", ria_validation_profile="popular-v1")
    with TestClient(create_app(settings, engine)) as client:
        assert started.wait(1)
        assert client.get("/health").status_code == 200
        assert not finished.is_set()
        assert client.get("/api/source-status").json()["valuation_check"]["request_cap"] == 96
    assert finished.is_set()


def test_invalid_profile_fails_before_any_startup_work(engine, monkeypatch):
    monkeypatch.setenv("RIA_VALIDATION_PROFILE", "unknown-profile")
    with pytest.raises(ValueError, match="validation profile"):
        create_app(Settings(str(engine.url), "fake-token", "x" * 32, ria_validation_profile="unknown-profile"), engine)
