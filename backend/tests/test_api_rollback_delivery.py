"""User-requested API rollback, with current paid guards and real adapter path.

Synthetic API and Telegram transports only; never proof of live delivery.
"""
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from backend import ria_ai_price as ai, ria_market_range
from backend.models import User
from backend.tests.test_monitor import p, add_search, drain, wake
from backend.tests.test_paid_sources_production import strict, approve
from backend.tests.test_ria_ai_price import wire
from backend.worker import TelegramSender


@pytest.mark.parametrize("unpaid_count", [0, 50])
def test_restored_production_adapter_serves_two_paid_searches_not_unpaid_first(p, monkeypatch, unpaid_count):
    strict(p)  # The first existing user (111) remains unpaid.
    add_search(p, sid=2, uid=222)
    add_search(p, sid=3, uid=333)
    approve(p, 222); approve(p, 333)
    for i in range(unpaid_count):
        add_search(p, sid=100+i, uid=1000+i, brand="BMW")
    p.runner.settings = replace(p.settings, ria_ai_price_enabled=True,
        ria_confirmed_deals_only=True, auto_ria_user_id="42")
    quotes = []
    def quote(key, uid, sid):
        quotes.append(sid)
        return ai.parse_quote(wire(15000), sid, now=p.clock[0])
    monkeypatch.setattr(ai, "fetch_quote", quote)
    # Do not monkeypatch RiaSearch.market_range: reproduce the actual hold.
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p); drain(p)
    assert quotes == ["124"]
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(222, "124"), (333, "124")]
    assert sum(path == "info" and params["auto_id"] == "124" for path, params in p.calls) == 1
    assert sum(path == "search" for path, _ in p.calls) == 1
    for _, car in p.sent:
        assert car.market == 13537.5
        assert car.valuation_evidence["basis"] == ai.API_BASIS
        text, _ = TelegramSender.card(car, historical_at=p.clock[0])
        assert "Оцінка API AUTO.RIA" in text
        assert "може відрізнятися" in text
    wake(p); drain(p)
    assert len(p.sent) == 2 and quotes == ["124"]


def test_restored_quote_does_not_send_after_stop(p, monkeypatch):
    strict(p); approve(p)
    p.runner.settings = replace(p.settings, ria_ai_price_enabled=True,
        ria_confirmed_deals_only=True, auto_ria_user_id="42")
    def quote(key, uid, sid):
        with Session(p.engine) as db:
            db.get(User, 111).ready = False
            db.commit()
        return ai.parse_quote(wire(15000), sid, now=p.clock[0])
    monkeypatch.setattr(ai, "fetch_quote", quote)
    drain(p); p.ads["124"] = p.clock[0] + 1; wake(p); drain(p)
    assert not p.sent


def test_rollback_policy_does_not_claim_native_identity():
    policy = ria_market_range.policy(confirmed_deals_only=True)
    assert policy["api_range_accepted_for_deal_selection"] is True
    assert policy["native_app_range_identity_verified"] is False
    assert policy["provider_calls_per_uncached_listing"] == 1
    assert policy["adjustment_percent"] == 5
