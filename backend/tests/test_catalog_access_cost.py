"""Filter dictionaries remain readable from cache without unpaid provider work."""
import time
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend import app as application, billing
from backend.app import create_app
from backend.billing_models import BillingControl, Entitlement
from backend.models import SourceBudget
from backend.ria_search import RiaSearch
from backend.tests.test_backend import setup, headers
from backend.tests.test_ria_search import fixture_fetch


def configured(setup, monkeypatch):
    engine, settings, _ = setup
    calls = []
    def factory(engine, key):
        return RiaSearch(engine, key, fixture_fetch(calls))
    monkeypatch.setattr(application, 'RiaSearch', factory)
    with Session(engine) as db:
        db.merge(BillingControl(id=billing.CONTROL, enforce=True, sales=False, offer={}))
        db.commit()
    return engine, replace(settings, auto_ria_api_key='fixture-not-a-key'), calls


def usage(engine):
    with Session(engine) as db:
        row = db.get(SourceBudget, 'auto_ria')
        return row.total, len(row.calls)


def test_unpaid_cold_catalog_does_not_dispatch_or_reserve_provider(setup, monkeypatch):
    engine, settings, calls = configured(setup, monkeypatch)
    client = TestClient(create_app(settings, engine))
    try:
        before = usage(engine)
        response = client.get('/api/catalog', headers=headers())
        assert response.status_code == 402
        assert response.json()['detail'] == 'paid_access_required'
        assert calls == [] and usage(engine) == before
    finally:
        client.close()


def test_paid_catalog_populates_shared_cache_unpaid_reads_it_without_http(setup, monkeypatch):
    engine, settings, calls = configured(setup, monkeypatch)
    client = TestClient(create_app(settings, engine))
    try:
        with Session(engine) as db:
            db.add(Entitlement(user_id=111, expires_at=time.time()+600, updated_at=time.time()))
            db.commit()
        first = client.get('/api/catalog', headers=headers(111))
        assert first.status_code == 200
        count = len(calls)
        assert count == 5
        before = usage(engine)
        second = client.get('/api/catalog', headers=headers(333))
        assert second.status_code == 200 and second.json() == first.json()
        assert len(calls) == count and usage(engine) == before
    finally:
        client.close()


def test_expiry_between_catalog_requests_stops_remaining_cold_calls(setup, monkeypatch):
    engine, settings, calls = configured(setup, monkeypatch)
    base = fixture_fetch(calls)
    def fetch(key, path, params):
        result = base(key, path, params)
        with Session(engine) as db:
            db.get(Entitlement, 111).expires_at = time.time()-1
            db.commit()
        return result
    monkeypatch.setattr(application, 'RiaSearch', lambda engine, key: RiaSearch(engine, key, fetch))
    client = TestClient(create_app(settings, engine))
    try:
        with Session(engine) as db:
            db.add(Entitlement(user_id=111, expires_at=time.time()+600, updated_at=time.time()))
            db.commit()
        before = usage(engine)
        response = client.get('/api/catalog', headers=headers())
        assert response.status_code == 402
        assert len(calls) == 1
        assert usage(engine) == (before[0]+1, before[1]+1)
    finally:
        client.close()
