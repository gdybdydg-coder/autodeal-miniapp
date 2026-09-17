import hashlib
import hmac
import json
import time
from dataclasses import replace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import Settings, create_app
from backend.auth import telegram_user
from backend.models import Car, Delivery, Filters, MonitorWatch, Search, User
from backend.worker import deliver_one, enqueue, ingest, matches
from backend.worker import TelegramSender
import httpx

TOKEN = "12345:TEST_ONLY_NOT_A_REAL_BOT_TOKEN"
SECRET = "test-only-webhook-secret-01234567890123456789"


def signed(uid=111, now=None, **extras):
    fields = {"auth_date": str(int(time.time()) if now is None else now),
              "user": json.dumps({"id": uid, "first_name": "Test"})}
    fields.update(extras)
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(key, "\n".join(f"{k}={fields[k]}" for k in sorted(fields)).encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def headers(uid=111):
    return {"X-Telegram-Init-Data": signed(uid)}


@pytest.fixture
def setup(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    settings = Settings(url, TOKEN, SECRET, True, True)
    with TestClient(create_app(settings, engine)) as client:
        yield engine, settings, client
    engine.dispose()


def command(client, text, update=1, uid=111, date=None):
    return client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                       json={"update_id": update, "message": {
                           "from": {"id": uid}, "chat": {"id": uid, "type": "private"},
                           "text": text, "date": date or int(time.time())}})


def subscribe(client, enabled=True, uid=111, **filters):
    return client.post("/api/subscriptions", headers=headers(uid),
                       json={"name": "BMW search", "filters": filters, "enabled": enabled})


def car(source_id="1", **values):
    data = dict(source="fixture", source_id=source_id, url="https://example.com/car/1",
                brand="BMW", model="3 Series", region="Київська область", body="Седан",
                fuel="Дизель", transmission="Автомат", year=2018, mileage=186000,
                price=15000, market=20000, comparables=10, observed_at=time.time())
    data.update(values)
    return Car(**data)


def ready(setup, **filters):
    engine, settings, client = setup
    assert command(client, "/start").status_code == 200
    result = subscribe(client, **filters)
    assert result.status_code == 200, result.text
    return result.json()["id"]


def test_signature():
    assert telegram_user(signed(now=1000), TOKEN, now=1001) == 111
    # HMAC method includes any signed extra fields.
    assert telegram_user(signed(now=1000, signature="signed-extra"), TOKEN, now=1001) == 111
    for bad in (signed(now=1000) + "&user=x", signed(now=1000).replace("111", "222"), ""):
        with pytest.raises(ValueError):
            telegram_user(bad, TOKEN, now=1001)
    with pytest.raises(ValueError):
        telegram_user(signed(now=1000), TOKEN, now=5000)
    with pytest.raises(ValueError):
        telegram_user(signed(now=5000), TOKEN, now=1000)
    with pytest.raises(ValueError):
        telegram_user(signed(uid=-1, now=1000), TOKEN, now=1000)


def test_ownership_and_bad_input(setup):
    _, _, client = setup
    assert client.get("/api/subscriptions").status_code == 401
    search_id = ready(setup)
    assert client.get("/api/subscriptions", headers=headers(222)).json() == []
    assert client.patch(f"/api/subscriptions/{search_id}", headers=headers(222), json={"enabled": False}).status_code == 404
    assert client.delete(f"/api/subscriptions/{search_id}", headers=headers(222)).status_code == 404
    assert subscribe(client, price={"from": 100, "to": 1}).status_code == 422
    response = client.post("/api/subscriptions", headers=headers(), json={
        "name": "x", "filters": {}, "enabled": True, "chat_id": 999})
    assert response.status_code == 422


def test_enable_gates(setup):
    engine, settings, client = setup
    assert subscribe(client).status_code == 409
    assert subscribe(client, enabled=False).status_code == 200
    with TestClient(create_app(replace(settings, delivery_enabled=False), engine)) as off:
        command(off, "/start")
        assert subscribe(off).status_code == 503
        assert off.get("/health").json()["delivery_available"] is False
    assert deliver_one(engine, replace(settings, source_ready=False), lambda *a: pytest.fail("network")) == "disabled"


def test_health_database_failure_and_recovery(setup, monkeypatch):
    from sqlalchemy.exc import OperationalError
    engine, settings, _ = setup
    with TestClient(create_app(replace(settings, delivery_enabled=False), engine)) as client:
        before = client.get("/health")
        assert before.status_code == 200
        assert before.json() == {"ok": True, "database": "connected",
                                 "database_type": "sqlite", "delivery_available": False}
        assert before.headers["cache-control"] == "no-store"
        with monkeypatch.context() as patch:
            def unavailable():
                raise OperationalError("SELECT 1", {}, Exception("password=SECRET host=PRIVATE"))
            patch.setattr(engine, "connect", unavailable)
            failed = client.get("/health")
            assert failed.status_code == 503
            assert failed.json() == {"ok": False, "database": "unavailable",
                                     "delivery_available": False}
            assert "SECRET" not in failed.text and "PRIVATE" not in failed.text
            assert failed.headers["cache-control"] == "no-store"
        assert client.get("/health").json()["database"] == "connected"


def test_health_is_read_only(setup):
    from sqlalchemy import event
    engine, _, client = setup
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.get("/health").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)


def test_duplicate_search_stop_and_replay(setup):
    engine, _, client = setup
    search_id = ready(setup, fuel=["Дизель", "Бензин"])
    assert subscribe(client, fuel=["Бензин", "Дизель"]).json()["id"] == search_id
    now = int(time.time())
    assert command(client, "/stop", update=2, date=now+1).status_code == 200
    command(client, "/start", update=1, date=now)
    assert subscribe(client).status_code == 409
    # Telegram can reset update ID after long inactivity; newer date still applies.
    command(client, "/start", update=0, date=now+604801)
    with Session(engine) as db:
        assert db.get(User, 111).ready is True
        assert db.get(Search, search_id).enabled is False
    assert client.post("/telegram/webhook", json={}).status_code == 403


def test_edit_renames_in_place_then_changed_filters_pause_and_invalidate_old_delivery(setup):
    engine, settings, client = setup
    sid = ready(setup, brand="BMW", fuel=["Дизель", "Бензин"])
    with Session(engine) as db:
        epoch = db.get(MonitorWatch, sid).epoch
    renamed = client.put(f"/api/subscriptions/{sid}", headers=headers(), json={
        "name": "  Моя BMW  ", "filters": {"brand": "BMW", "fuel": ["Бензин", "Дизель", "Дизель"]}})
    assert renamed.status_code == 200 and renamed.json()["enabled"]
    assert renamed.json()["id"] == sid and renamed.json()["name"] == "Моя BMW"
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid).epoch == epoch
    ingest(engine, [car("before-edit")]); enqueue(engine)
    changed = client.put(f"/api/subscriptions/{sid}", headers=headers(), json={
        "name": "Інша BMW", "filters": {"brand": "BMW", "price": {"from": 0, "to": 16000},
            "year": {"from": 2017, "to": 2020}, "mileage": {"from": 100, "to": 200},
            "region": "Київська область", "fuel": ["Дизель"], "body": ["Седан"], "transmission": ["Автомат"]}})
    assert changed.status_code == 200 and not changed.json()["enabled"]
    assert changed.json()["filters"]["price"] == {"from": 0, "to": 16000}
    assert len(client.get("/api/subscriptions", headers=headers()).json()) == 1
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid) is None
    assert deliver_one(engine, settings, lambda *a: pytest.fail("old filter sent a message")) == "cancelled"
    assert client.patch(f"/api/subscriptions/{sid}", headers=headers(), json={"enabled": True}).status_code == 200
    with Session(engine) as db:
        assert db.get(MonitorWatch, sid).epoch != epoch


def test_edit_checks_identity_ownership_validation_and_cannot_enable(setup):
    _, _, client = setup
    sid = subscribe(client, enabled=False).json()["id"]
    path = f"/api/subscriptions/{sid}"
    body = {"name": "Зміни", "filters": {"brand": "Volkswagen"}}
    assert client.put(path, json=body).status_code == 401
    assert client.put(path, headers=headers(222), json=body).status_code == 404
    for bad in ({**body, "enabled": True}, {**body, "name": "  "},
                {**body, "filters": {"price": {"from": 2000, "to": 1}}}):
        assert client.put(path, headers=headers(), json=bad).status_code == 422
    assert client.get("/api/subscriptions", headers=headers()).json()[0]["name"] == "BMW search"
    assert client.delete(path, headers=headers()).status_code == 204
    assert client.put(path, headers=headers(), json=body).status_code == 404


def test_duplicate_edit_and_duplicate_draft_cannot_pause_or_overwrite_active_subscription(setup):
    engine, _, client = setup
    sid = ready(setup, brand="BMW")
    other = subscribe(client, enabled=False, brand="Audi").json()["id"]
    response = client.put(f"/api/subscriptions/{sid}", headers=headers(), json={
        "name": "Duplicate", "filters": {"brand": "Audi"}})
    assert response.status_code == 409 and response.json()["detail"] == "Subscription filters already exist"
    assert subscribe(client, enabled=False, brand="BMW").status_code == 409
    with Session(engine) as db:
        row = db.get(Search, sid)
        assert row.enabled and row.filters["brand"] == "BMW" and row.name == "BMW search"
        assert db.get(Search, other).filters["brand"] == "Audi"


def test_twenty_saved_subscriptions_can_be_edited_without_consuming_a_new_slot(setup):
    _, _, client = setup
    ids = [subscribe(client, enabled=False, price={"to": 10000+i}).json()["id"] for i in range(20)]
    assert len(set(ids)) == 20
    assert subscribe(client, enabled=False, price={"to": 20000}).status_code == 409
    edited = client.put(f"/api/subscriptions/{ids[0]}", headers=headers(), json={
        "name": "Оновлена", "filters": {"price": {"to": 20000}}})
    assert edited.status_code == 200 and edited.json()["id"] == ids[0]
    assert len(client.get("/api/subscriptions", headers=headers()).json()) == 20
    assert subscribe(client, uid=222, enabled=False, price={"to": 20000}).status_code == 200
    cors = client.options(f"/api/subscriptions/{ids[0]}", headers={
        "Origin": "https://gdybdydg-coder.github.io", "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data,Content-Type"})
    assert cors.status_code == 200


def test_matching():
    c = car()
    assert matches(c, Filters(mileage={"from": 186, "to": 186}, fuel=["Газ", "Дизель"]))
    assert not matches(c, Filters(mileage={"from": 187}))
    assert not matches(c, Filters(transmission=["Робот"]))
    assert not matches(car(price=18000), Filters(onlyDeals=False))
    assert matches(car(price=17000), Filters())


def test_dedupe_and_delivery(setup):
    engine, settings, client = setup
    ready(setup)
    assert subscribe(client, brand="BMW").status_code == 200
    ingest(engine, [car()])
    enqueue(engine)
    enqueue(engine)
    with Session(engine) as db:
        assert len(list(db.scalars(select(Delivery)))) == 1
    calls = []
    def sender(uid, listing):
        calls.append(uid)
        return {"ok": True, "result": {"message_id": 55}}
    assert deliver_one(engine, settings, sender) == "sent"
    enqueue(engine)
    assert deliver_one(engine, settings, sender) == "empty"
    assert calls == [111]


def test_no_backlog_stale_or_deleted_subscription(setup):
    engine, settings, client = setup
    ingest(engine, [car("old")])
    search_id = ready(setup)
    ingest(engine, [car("stale", observed_at=time.time()-90000)])
    enqueue(engine)
    assert deliver_one(engine, settings, lambda *a: pytest.fail("network")) == "empty"
    ingest(engine, [car("new")])
    enqueue(engine)
    client.delete(f"/api/subscriptions/{search_id}", headers=headers())
    assert deliver_one(engine, settings, lambda *a: pytest.fail("network")) == "cancelled"


@pytest.mark.parametrize("result,state", [
    ({"uncertain": True}, "uncertain"),
    ({"error_code": 500}, "uncertain"),
    ({"error_code": 400}, "failed"),
    ({"error_code": 403}, "failed"),
])
def test_non_retry_outcomes(setup, result, state):
    engine, settings, _ = setup
    ready(setup)
    ingest(engine, [car()])
    enqueue(engine)
    assert deliver_one(engine, settings, lambda *a: result) == state
    assert deliver_one(engine, settings, lambda *a: pytest.fail("duplicate")) == "empty"


def test_rate_limit_and_stop(setup):
    engine, settings, client = setup
    ready(setup)
    ingest(engine, [car()])
    enqueue(engine)
    now = time.time()
    result = {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}
    assert deliver_one(engine, settings, lambda *a: result, now=now) == "pending"
    assert deliver_one(engine, settings, lambda *a: pytest.fail("too early"), now=now+1) == "empty"
    command(client, "/stop", update=2)
    assert deliver_one(engine, settings, lambda *a: pytest.fail("stopped"), now=now+31) == "empty"


def test_cors(setup):
    client = setup[2]
    good = client.options("/api/subscriptions", headers={
        "Origin": "https://gdybdydg-coder.github.io",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data"})
    assert good.status_code == 200
    bad = client.options("/api/subscriptions", headers={
        "Origin": "https://attacker.example", "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 400


def test_sender_uses_verified_target_and_photo(monkeypatch):
    requests = []
    real_client = httpx.Client
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    monkeypatch.setattr("backend.worker.httpx.Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    result = TelegramSender(TOKEN)(111, car(photo="https://example.com/test.jpg"))
    assert result["result"]["message_id"] == 7
    assert requests[0].url.path.endswith("/sendPhoto")
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == 111
    assert "/stop" in payload["caption"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://example.com/car/1"
