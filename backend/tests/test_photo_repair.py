from dataclasses import replace
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import photo_repair
from backend.models import Delivery, DeliveryTiming, Listing, MonitorWatch, Search, SourceProbe, User
from backend.tests.test_monitor import p
from backend.tests.test_failed_delivery_recovery import prepare

URL = 'https://cdn0.riastatic.com/photosnew/auto/photo/test__123m.jpg'


def configured(p, monkeypatch, state='sent', receipt=True):
    prepare(p, monkeypatch, state)
    p.runner.settings = replace(p.runner.settings, ria_failed_delivery_recovery_id='', ria_photo_repair_ids='124')
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        listing = db.get(Listing, delivery.listing_id)
        listing.car = {**listing.car, 'photo': URL}
        if receipt:
            db.merge(SourceProbe(id='telegram-delivery-result-v1-' + str(delivery.id), requests=0, status='sent',
                checked_at=p.clock[0], result={'attempts': [{'method': 'sendMessage', 'accepted': True}]}))
        db.commit()


def run(p, reply):
    class Sender:
        def add_photo(self, uid, mid, car, *, historical_at=None):
            return reply(uid, mid, car)
    assert p.runner.claim()
    try:
        photo_repair.run_once(p.runner, Sender())
    finally:
        p.runner.release('idle')


def test_owner_text_card_edited_once_without_resetting_receipts_epochs_or_other_users(p, monkeypatch):
    configured(p, monkeypatch)
    calls = []
    with Session(p.engine) as db:
        before = [(d.id, d.state, d.message_id) for d in db.scalars(select(Delivery))]
        epoch = db.get(MonitorWatch, 1).epoch
        timing = db.get(DeliveryTiming, before[0][0]).accepted_at
    def edit(uid, mid, car):
        calls.append((uid, mid))
        return {'ok': True, 'result': {'message_id': mid, 'photo': [{}]}}
    run(p, edit)
    run(p, lambda *_: pytest.fail('repeated edit'))
    assert calls == [(111, 71)]
    with Session(p.engine) as db:
        assert [(d.id, d.state, d.message_id) for d in db.scalars(select(Delivery))] == before
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(DeliveryTiming, before[0][0]).accepted_at == timing
        assert db.get(SourceProbe, 'owner-photo-repair-v1-111-124').status == 'edited'


@pytest.mark.parametrize('state', ['failed', 'uncertain', 'pending', 'sending', 'cancelled'])
def test_non_sent_claims_never_edited(p, monkeypatch, state):
    configured(p, monkeypatch, state)
    run(p, lambda *_: pytest.fail('not a sent card'))


@pytest.mark.parametrize('blocker', ['stopped', 'not_ready', 'changed_filter', 'no_admin', 'already_photo'])
def test_photo_repair_respects_consent_and_only_confirmed_text_cards(p, monkeypatch, blocker):
    configured(p, monkeypatch, receipt=blocker != 'already_photo')
    with Session(p.engine) as db:
        if blocker == 'stopped': db.get(Search, 1).enabled = False
        if blocker == 'not_ready': db.get(User, 111).ready = False
        if blocker == 'changed_filter': db.get(Search, 1).fingerprint = 'changed'
        db.commit()
    if blocker == 'no_admin': p.runner.settings = replace(p.runner.settings, admin_telegram_id=0)
    run(p, lambda *_: pytest.fail('ineligible edit'))


def test_uncertain_edit_is_not_repeated(p, monkeypatch):
    configured(p, monkeypatch)
    run(p, lambda *_: {'uncertain': True})
    run(p, lambda *_: pytest.fail('uncertain edit repeated'))
    with Session(p.engine) as db:
        assert db.get(SourceProbe, 'owner-photo-repair-v1-111-124').status == 'uncertain'
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == 'sent'


def test_missing_photo_is_refreshed_once_with_accounted_provider_call(p, monkeypatch):
    from backend.models import SourceBudget
    configured(p, monkeypatch)
    with Session(p.engine) as db:
        delivery = db.scalar(select(Delivery).where(Delivery.user_id == 111))
        listing = db.get(Listing, delivery.listing_id)
        listing.car = {**listing.car, 'photo': None}
        before = db.get(SourceBudget, 'auto_ria').total
        db.commit()
    old = p.runner.search_factory
    def factory(*args):
        source = old(*args)
        fetch = source.fetch
        def with_photo(key, path, params):
            data = fetch(key, path, params)
            if path == 'info': data['photoData'] = {'seoLinkF': URL}
            return data
        source.fetch = with_photo
        return source
    p.runner.search_factory = factory
    calls = []
    run(p, lambda uid, mid, car: calls.append(str(car.photo)) or
        {'ok': True, 'result': {'message_id': mid, 'photo': [{}]}})
    run(p, lambda *_: pytest.fail('repeat'))
    assert calls == [URL]
    with Session(p.engine) as db:
        assert db.get(SourceBudget, 'auto_ria').total == before + 1
        assert db.get(SourceProbe, 'owner-photo-repair-v1-111-124').requests == 1


@pytest.mark.parametrize('value', ['1,,2', '1,1', 'a', '1,2,3,4,5,6'])
def test_invalid_repair_selector_is_rejected(value):
    with pytest.raises(ValueError): photo_repair.ids(value)


def test_failed_preflight_can_retry_once_but_never_repeats_issued_edit(p, monkeypatch):
    configured(p, monkeypatch)
    run(p, lambda *_: {'ok': False, 'photo_unavailable': True})
    run(p, lambda *_: pytest.fail('retry too soon'))
    p.clock[0] += 61
    run(p, lambda uid, mid, car: {'ok': True, 'result': {'message_id': mid, 'photo': [{}]}})
    p.clock[0] += 61
    run(p, lambda *_: pytest.fail('issued edit must not repeat'))
    with Session(p.engine) as db:
        probe = db.get(SourceProbe, 'owner-photo-repair-v1-111-124')
        assert probe.status == 'edited' and probe.result['attempts'] == 2


def test_preflight_retry_is_bounded_to_two_attempts(p, monkeypatch):
    configured(p, monkeypatch)
    run(p, lambda *_: {'ok': False, 'photo_unavailable': True})
    p.clock[0] += 61
    run(p, lambda *_: {'ok': False, 'photo_unavailable': True})
    p.clock[0] += 61
    run(p, lambda *_: pytest.fail('preflight retry exhausted'))


def test_legacy_stale_formatter_failure_can_resume_without_changing_sent_claim(p, monkeypatch):
    configured(p, monkeypatch)
    p.clock[0] += 1000
    with Session(p.engine) as db:
        db.add(SourceProbe(id='owner-photo-repair-v1-111-124', status='editing', requests=0,
            checked_at=p.clock[0], result={'source_id':'124', 'message_id':71, 'attempts':2}))
        db.commit()
    run(p, lambda uid, mid, car: {'ok':True, 'result':{'message_id':mid,'photo':[{}]}})
    with Session(p.engine) as db:
        assert db.get(SourceProbe, 'owner-photo-repair-v1-111-124').status == 'edited'
        assert db.scalar(select(Delivery).where(Delivery.user_id == 111)).state == 'sent'


def test_prepared_edit_claim_cannot_be_replayed_even_when_quote_is_old(p, monkeypatch):
    configured(p, monkeypatch)
    p.clock[0] += 1000
    with Session(p.engine) as db:
        db.add(SourceProbe(id='owner-photo-repair-v1-111-124', status='editing', requests=0,
            checked_at=p.clock[0], result={'source_id':'124','message_id':71,'attempts':2,'prepared':True}))
        db.commit()
    run(p, lambda *_: pytest.fail('possibly issued edit must not replay'))
