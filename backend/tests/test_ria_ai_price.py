"""Provider transport/schema and the production monitor-to-dispatch path."""
import copy
import json
from dataclasses import replace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import ria_ai_price as ai, ria_market_range as market_range
from backend.app import create_app
from backend.auto_ria import RiaError
from backend.models import Delivery, Listing, MonitorJob, MonitorSeen, Search, SourceBudget, SourceCache, SourceProbe, User
from backend.tests.test_monitor import p, add_search, drain, wake, details
from backend.worker import deliver_one, enqueue, fresh


def wire(mean=5423, radius=.05):
    # Actual allowlisted statisticData shape observed 2026-09-18 for 40292766.
    return {"statisticData": [{"id": "avgPriceBlock", "name": "Середня ціна",
        "type": "avgPrice", "price": {"USD": mean, "UAH": 243425},
        "avgValueRange": radius, "quantityAdv": 261}],
        "similarCars": [{"VIN": "must-not-retain", "userId": "private-seller"}],
        "noticeData": [{"noticeString": "must-not-retain"}]}


def enable(p, monkeypatch, response=None, error=None, action=None):
    p.runner.settings = replace(p.settings, ria_ai_price_enabled=True, auto_ria_user_id="42")
    calls = []
    def fetch(key, user_id, sid):
        assert key == "test-only" and user_id == "42"
        calls.append(sid)
        if action:
            action()
        if error:
            raise RiaError(error)
        return ai.parse_quote(wire(15000) if response is None else response, sid)
    monkeypatch.setattr(ai, "fetch_quote", fetch)
    monkeypatch.setattr("backend.ria_search.RiaSearch.notification_comparisons",
                        lambda *a: pytest.fail("new notifications must not request comparables"))
    return calls


def discover_new(p, sid="124"):
    p.ads[sid] = p.clock[0] + 1
    wake(p)


def test_live_shape_uses_both_provider_fields_and_discards_private_response():
    quote = ai.parse_quote(wire(), "40292766", now=1800000000)
    assert (quote["lower_usd"], quote["upper_usd"]) == (5151, 5694)
    assert quote["provider"] == {"average_usd": 5423, "range_fraction": .05,
                                  "quantity": 261, "period_hours": 168}
    assert "must-not-retain" not in json.dumps(quote) and "private-seller" not in json.dumps(quote)
    # Provider width and owner's discount are independent, not a fixed 10% cut.
    wider = ai.parse_quote(wire(10000, .1), "124", now=1800000000)
    assert wider["lower_usd"] == 9000
    assert market_range.range_valid(wider, "124", 1800000000)
    wider["lower_usd"] += 1
    assert not market_range.range_valid(wider, "124", 1800000000)


@pytest.mark.parametrize("change", [{"avgValueRange": None}, {"avgValueRange": True},
    {"avgValueRange": "0.05"}, {"avgValueRange": 5}, {"avgValueRange": -1},
    {"avgValueRange": float("nan")}, {"price": {"UAH": 200000}},
    {"price": {"USD": 0}}, {"price": {"USD": float("inf")}},
    {"quantityAdv": 0}, {"quantityAdv": True}])
def test_invalid_or_missing_fields_do_not_invent_a_default_range(change):
    data = wire()
    data["statisticData"][0].update(change)
    assert ai.parse_quote(data, "124") is None


def test_ambiguous_average_and_average_without_radius_are_unpriced():
    data = wire()
    data["statisticData"].append(copy.deepcopy(data["statisticData"][0]))
    assert ai.parse_quote(data, "124") is None
    data = wire()
    del data["statisticData"][0]["avgValueRange"]
    assert ai.parse_quote(data, "124") is None


def test_transport_uses_documented_paid_post_no_redirect_and_bounded_body(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == ai.MAX_BYTES + 1
            return json.dumps(wire()).encode()
    class Opener:
        def open(self, request, timeout):
            url = urlparse(request.full_url)
            assert url.scheme == "https" and url.netloc == "developers.ria.com"
            assert url.path == "/auto/ai-avarage-price/"
            assert parse_qs(url.query) == {"api_key": ["test-secret"], "user_id": ["42"]}
            assert request.method == "POST" and timeout == 20
            assert json.loads(request.data) == {"langId": 4, "period": 168, "params": {"omniId": "124"}}
            return Response()
    def opener(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "https://example.com") is None
        return Opener()
    monkeypatch.setattr(ai, "build_opener", opener)
    quote = ai.fetch_quote("test-secret", "42", "124")
    assert quote["lower_usd"] == 5151 and "test-secret" not in json.dumps(quote)


@pytest.mark.parametrize("error,code", [(URLError("secret-url"), "ai_connection_error"),
    (TimeoutError("secret-url"), "ai_connection_error"),
    *[(HTTPError("secret-url", status, "secret", {}, None), code) for status, code in
      [(403, "ai_access_denied"), (401, "ai_access_denied"), (429, "quota_exceeded"), (500, "ai_upstream_error")]]])
def test_transport_errors_never_disclose_credentials(monkeypatch, error, code):
    class Opener:
        def open(self, *args, **kwargs): raise error
    monkeypatch.setattr(ai, "build_opener", lambda *a: Opener())
    with pytest.raises(RiaError) as caught:
        ai.fetch_quote("test-secret", "42", "124")
    assert str(caught.value) == code


def test_quote_cache_shares_budget_and_has_no_private_data(p, monkeypatch):
    calls = enable(p, monkeypatch)
    with Session(p.engine) as db:
        baseline = db.get(SourceBudget, "auto_ria").total
    source = p.runner.search_factory(p.engine, "test-only")
    source.acquire()
    try:
        a = source.market_range("124", "42")
        assert source.market_range("124", "42") == a
        assert calls == ["124"] and source.requests_made == 1
    finally:
        source.release()
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == baseline + 1
        assert "private-seller" not in json.dumps(db.scalar(select(SourceCache)).payload)


def test_new_listing_one_quote_no_old_car_and_no_duplicate(p, monkeypatch):
    calls = enable(p, monkeypatch)
    drain(p)
    assert not calls and not details(p, "123")
    discover_new(p)
    drain(p)
    assert calls == ["124"]
    assert [(uid, car.source_id) for uid, car in p.sent] == [(111, "124")]
    car = p.sent[0][1]
    assert car.market == 13537.5 and car.comparables == 0
    assert fresh(car, p.clock[0], require_provider_range=True)
    wake(p)
    drain(p)
    assert len(p.sent) == 1 and calls == ["124"]


def test_subscription_threshold_uses_adjusted_lower_bound(p, monkeypatch):
    calls = enable(p, monkeypatch)
    add_search(p, minDiscount=20)
    p.prices["124"] = 11500  # 15.05% below 13537.5; 23.33% below raw 15000.
    drain(p)
    discover_new(p)
    drain(p)
    assert calls == ["124"]
    assert [uid for uid, _ in p.sent] == [111]


@pytest.mark.parametrize("failure", ["ai_access_denied", "ai_connection_error", "ai_invalid_response", "quota_exceeded", "missing_range"])
def test_unavailable_quote_keeps_fresh_informational_alert_and_no_peer_fallback(p, monkeypatch, failure):
    calls = enable(p, monkeypatch, response={} if failure == "missing_range" else None,
                   error=None if failure == "missing_range" else failure)
    drain(p)
    discover_new(p)
    drain(p)
    assert calls == ["124"] and len(p.sent) == 1
    car = p.sent[0][1]
    assert car.market is None and fresh(car, p.clock[0], require_provider_range=True)
    assert "provider_market_range_unavailable" in car.valuation_evidence["uncertainty_reasons"]
    with Session(p.engine) as db:
        if failure == "ai_access_denied":
            assert db.get(SourceBudget, "auto_ria").blocked_until == 0
        assert db.scalar(select(Delivery)).state == "sent"


def test_stop_during_ai_request_prevents_delivery(p, monkeypatch):
    def stop():
        with Session(p.engine) as db:
            db.get(User, 111).ready = False
            db.get(Search, 1).enabled = False
            db.commit()
    enable(p, monkeypatch, action=stop)
    drain(p)
    discover_new(p)
    drain(p)
    assert not p.sent


@pytest.mark.parametrize("state", ["sent", "uncertain"])
def test_policy_switch_never_reopens_existing_delivery_claims(p, monkeypatch, state):
    drain(p)
    discover_new(p)
    drain(p)
    with Session(p.engine) as db:
        db.scalar(select(Delivery)).state = state
        db.commit()
    calls = enable(p, monkeypatch)
    wake(p)
    drain(p)
    assert len(p.sent) == 1 and calls == []
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == state


def test_pending_old_policy_is_revalidated_only_for_current_unsent_interests(p, monkeypatch):
    drain(p)
    discover_new(p)
    assert p.runner.tick()  # discover
    assert p.runner.tick()  # evaluate old peer policy, no dispatch yet
    enqueue(p.engine)
    with Session(p.engine) as db:
        assert db.scalar(select(Listing)).car["market"] == 15000
    calls = enable(p, monkeypatch)
    assert deliver_one(p.engine, p.runner.settings, p.runner.sender) == "pending"
    assert not p.sent
    wake(p, 5)
    drain(p)
    wake(p, 5)  # Dispatch's retry deadline may follow the in-flight refresh.
    drain(p)
    assert calls == ["124"] and len(p.sent) == 1 and p.sent[0][1].market == 13537.5
    with Session(p.engine) as db:
        assert db.get(MonitorSeen, (1, "123")) is None


def test_read_only_probe_is_once_budgeted_and_never_creates_messages(p, monkeypatch):
    calls = enable(p, monkeypatch)
    with Session(p.engine) as db:
        baseline = db.get(SourceBudget, "auto_ria").total
    ai.check_once(p.engine, "test-only", "42", "124")
    ai.check_once(p.engine, "test-only", "42", "124")
    assert calls == ["124"] and not p.sent
    with Session(p.engine) as db:
        probe = db.get(SourceProbe, "auto-ria-ai-range-v1-124")
        assert probe.status == "verified" and probe.requests == 1
        assert db.get(SourceBudget, "auto_ria").total == baseline + 1
        assert db.scalar(select(MonitorJob)) is None and db.scalar(select(Delivery)) is None


def test_enabled_mode_requires_server_account_id_without_exposing_credentials(p):
    with pytest.raises(ValueError, match="server-only AUTO.RIA AI credentials"):
        create_app(replace(p.settings, ria_ai_price_enabled=True), p.engine)
    assert "test-only" not in repr(replace(p.settings, auto_ria_user_id="42"))


@pytest.mark.parametrize("repair", [False, True])
def test_missing_comparison_dimensions_do_not_hide_provider_pricing(p, monkeypatch, repair):
    calls = enable(p, monkeypatch)
    factory = p.runner.search_factory
    def sparse(engine, key):
        source = factory(engine, key)
        original = source.car
        def car(sid, **kw):
            candidate = original(sid, **kw)
            candidate.update(generation_id=None, modification_id=None, body_id=None, mileage=None)
            if repair:
                candidate.update(comparable_condition=False, condition_exclusions=["technical_condition"])
            return candidate
        source.car = car
        return source
    p.runner.search_factory = sparse
    drain(p)
    discover_new(p)
    drain(p)
    assert calls == ["124"] and len(p.sent) == 1
    car = p.sent[0][1]
    assert car.market == 13537.5 and fresh(car, p.clock[0], require_provider_range=True)
    assert car.valuation_evidence["condition_notices"] == (["technical_condition"] if repair else [])


def test_above_reference_candidate_and_nonmatching_price_filter_are_not_deals(p, monkeypatch):
    calls = enable(p, monkeypatch)
    p.prices["124"] = 14500  # Below source mean but above adjusted lower reference.
    drain(p)
    discover_new(p)
    drain(p)
    assert calls == ["124"] and not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "checked"


def test_quote_failure_still_consumes_budget(p, monkeypatch):
    calls = enable(p, monkeypatch, error="ai_connection_error")
    with Session(p.engine) as db:
        baseline = db.get(SourceBudget, "auto_ria").total
    source = p.runner.search_factory(p.engine, "test-only")
    source.acquire()
    try:
        with pytest.raises(RiaError, match="ai_connection_error"):
            source.market_range("124", "42")
    finally:
        source.release()
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == baseline + 1
    assert calls == ["124"]
