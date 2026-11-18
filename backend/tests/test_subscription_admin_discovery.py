"""The account page exposes management only for the verified Telegram owner."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from backend.app import Settings, create_app
from backend.models import Base
from backend.tests.test_backend import TOKEN, SECRET, headers


@pytest.mark.parametrize('actor,expected_owner,can_manage', [
    (111, '111', True), (222, '111', False), (111, '222', False), (111, '', False),
])
def test_status_identity_and_management_capability_are_server_verified(tmp_path, monkeypatch, actor, expected_owner, can_manage):
    monkeypatch.setenv('SUBSCRIPTION_EXPECTED_ADMIN_ID', expected_owner)
    engine=create_engine('sqlite:///'+str(tmp_path/'capabilities.db'),connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    settings=Settings('unused',TOKEN,SECRET,admin_telegram_id=111)
    client=TestClient(create_app(settings,engine))
    try:
        result=client.get('/api/billing/status?user_id=111',headers=headers(actor))
        assert result.status_code==200
        assert result.json()['telegram_id']==actor
        assert result.json()['can_manage_subscriptions'] is can_manage
        assert client.get('/api/billing/status').status_code==401
    finally:
        client.close();engine.dispose()
