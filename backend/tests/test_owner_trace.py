import json
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from backend import owner_trace
from backend.models import Delivery, DeliveryTiming, Search, SourceBudget, MonitorJob
from backend.tests.test_monitor import p, drain, wake


@pytest.mark.parametrize("delivery_state", ["sent", "uncertain"])
def test_trace_reports_only_owners_receipt_and_preserves_all_state(p, delivery_state):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        own = db.scalar(select(Delivery))
        own.state = delivery_state
        own.message_id = 12345
        db.add(Delivery(user_id=222, listing_id=own.listing_id, state="sent", message_id=99999))
        db.commit()
        spent = db.get(SourceBudget, "auto_ria").total
    calls, sent = len(p.calls), len(p.sent)
    writes = []
    def capture(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(sql)
    event.listen(p.engine, "before_cursor_execute", capture)
    try:
        with Session(p.engine) as db:
            report = owner_trace.snapshot(db, 111, "124")
            assert report["trace"]["state"] == "observed"
            assert report["delivery"]["state"] == delivery_state
            assert report["delivery"]["message_id"] == 12345
            assert report["delivery"]["timing"]["accepted_at"] is not None
            assert len(report["searches"]) == 1 and report["searches"][0]["search_id"] == 1
            assert report["searches"][0]["monitor"]["cursor_at"] is not None
            assert report["activity"]["messages_accepted"] == (1 if delivery_state == "sent" else 0)
            assert [r["message_id"] for r in report["recent_accepted"]] == ([12345] if delivery_state == "sent" else [])
            assert "99999" not in json.dumps(report) and "user_id" not in json.dumps(report)
            assert db.get(SourceBudget, "auto_ria").total == spent
    finally:
        event.remove(p.engine, "before_cursor_execute", capture)
    assert not writes and (len(p.calls), len(p.sent)) == (calls, sent)


def test_recent_receipts_are_bounded_ordered_and_private(p):
    drain(p)
    p.ads.update({str(n): p.clock[0] + 1 for n in range(124, 132)})
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        own = list(db.scalars(select(Delivery).where(Delivery.user_id == 111).order_by(Delivery.id)))
        assert len(own) == 8
        for n, row in enumerate(own):
            db.get(DeliveryTiming, row.id).accepted_at = p.clock[0] + n
        other = Delivery(user_id=222, listing_id=own[0].listing_id, state="sent", message_id=99999)
        db.add(other)
        db.flush()
        db.add(DeliveryTiming(delivery_id=other.id, queued_at=p.clock[0], accepted_at=p.clock[0] + 100))
        db.commit()
        report = owner_trace.snapshot(db, 111, "124")
        assert len(report["recent_accepted"]) == 5
        assert [r["message_id"] for r in report["recent_accepted"]] == [row.message_id for row in reversed(own[-5:])]
        assert report["activity"]["messages_accepted"] == 8
        assert "99999" not in json.dumps(report)


def test_trace_explains_owner_threshold_without_evaluating_again(p):
    with Session(p.engine) as db:
        search = db.get(Search, 1)
        search.filters = {**search.filters, "minDiscount": 40}
        db.commit()
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    before = len(p.calls)
    with Session(p.engine) as db:
        report = owner_trace.snapshot(db, 111, "124")
        assert report["trace"]["subscriptions"][0]["state"] == "below_min_discount"
        assert report["searches"][0]["filters"]["minDiscount"] == 40
        assert report["job"]["market"] == 15000
        assert report["delivery"] is None
    assert len(p.calls) == before and not p.sent


def test_unknown_admin_does_not_fall_back_to_another_user(p):
    with Session(p.engine) as db:
        assert owner_trace.snapshot(db, 999, "124") == {
            "source_id": "124", "scope": "configured_admin", "account_found": False}
        assert owner_trace.snapshot(db, 0, "124") is None


def test_only_explicit_admin_trace_runs_and_failure_cannot_block_startup(p, monkeypatch, caplog):
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError("must-not-log-connection-url-or-secrets")
    monkeypatch.setattr(owner_trace, "snapshot", fail)
    owner_trace.log_once(p.engine, p.settings)
    owner_trace.log_once(p.engine, replace(p.settings, ria_owner_trace_listing_id="124"))
    assert not calls
    owner_trace.log_once(p.engine, replace(p.settings, admin_telegram_id=111, ria_owner_trace_listing_id="124"))
    assert len(calls) == 1 and "RuntimeError" in caplog.text
    assert "must-not-log" not in caplog.text


def test_report_omits_seller_data_and_does_not_reveal_unrelated_search_names(p, caplog):
    drain(p)
    p.ads["124"] = p.clock[0] + 1
    wake(p)
    drain(p)
    owner_trace.log_once(p.engine, replace(p.settings, admin_telegram_id=111, ria_owner_trace_listing_id="124"))
    assert "Owner listing trace" in caplog.text
    assert all(key not in caplog.text for key in ("VIN", "api_key", "phone", "test-token", "fingerprint", "epoch"))


def test_retained_provider_price_proof_is_exact_bounded_and_has_no_new_source_calls(p):
    drain(p)
    p.ads['124'] = p.clock[0] + 1
    wake(p)
    drain(p)
    with Session(p.engine) as db:
        job = db.get(MonitorJob, '124')
        saved = dict(job.result)
        rating = dict(saved['rating'])
        rating.update(market=6327.95, discount=21,
            valuation_evidence={'version': 'autoria-lower-bound-v1', 'source_range': {
                'source_id': '124', 'basis': 'auto_ria_ai_market_range', 'currency': 'USD',
                'lower_usd': 6661, 'upper_usd': 7363, 'observed_at': p.clock[0],
                'provider': {'average_usd': 7012, 'range_fraction': .05, 'quantity': 42,
                             'period_hours': 168, 'api_key': 'PRIVATE_KEY'},
                'similarCars': ['PRIVATE_SELLER'], 'VIN': 'PRIVATE_VIN'}})
        saved['rating'] = rating
        job.result = saved
        db.commit()
    calls, sent = len(p.calls), len(p.sent)
    writes = []
    def capture(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')): writes.append(sql)
    event.listen(p.engine, 'before_cursor_execute', capture)
    try:
        with Session(p.engine) as db:
            report = owner_trace.snapshot(db, 111, '124')
            proof = report['job']['pricing_evidence']
            assert proof['source_range']['lower_usd'] == 6661
            assert proof['provider'] == {'average_usd': 7012, 'range_fraction': .05, 'quantity': 42, 'period_hours': 168}
            assert 'PRIVATE_' not in json.dumps(report)
            assert report['stored_card']['brand'] == 'Volkswagen'
    finally:
        event.remove(p.engine, 'before_cursor_execute', capture)
    assert not writes and (len(p.calls), len(p.sent)) == (calls, sent)
