"""Owner media repair obeys current access; fake API/edit and isolated SQLite."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import billing, photo_repair
from backend.billing_models import BillingControl, Entitlement
from backend.models import Delivery, Listing, SourceBudget, SourceProbe
from backend.tests.test_monitor import p
from backend.tests.test_photo_repair import configured, run, URL


def paid(p, *, expires_in=3600, missing_photo=False):
    with Session(p.engine) as db:
        db.merge(BillingControl(id=billing.CONTROL, sales=True, enforce=True, offer={}))
        db.merge(Entitlement(user_id=111, expires_at=p.clock[0]+expires_in, updated_at=p.clock[0]))
        if missing_photo:
            delivery=db.scalar(select(Delivery).where(Delivery.user_id==111))
            listing=db.get(Listing, delivery.listing_id)
            listing.car={**listing.car,"photo":None}
        before=db.get(SourceBudget,"auto_ria").total
        db.commit()
    return before


def expire(p):
    with Session(p.engine) as db:
        db.get(Entitlement,111).expires_at=p.clock[0]
        db.commit()


def assert_sent_unchanged(p):
    with Session(p.engine) as db:
        row=db.scalar(select(Delivery).where(Delivery.user_id==111))
        assert row.state=="sent" and row.message_id==71


@pytest.mark.parametrize("missing_photo",[False,True])
def test_expired_owner_spends_no_provider_calls_or_edit(p,monkeypatch,missing_photo):
    configured(p,monkeypatch)
    before=paid(p,expires_in=0,missing_photo=missing_photo)
    p.runner.search_factory=lambda *_:pytest.fail("expired owner constructed provider")
    run(p,lambda *_:pytest.fail("expired owner edit"))
    with Session(p.engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==before
        assert db.get(SourceProbe,"owner-photo-repair-v1-111-124") is None
    assert_sent_unchanged(p)


def test_expiry_after_source_construction_is_rechecked_before_paid_read(p,monkeypatch):
    configured(p,monkeypatch)
    before=paid(p,missing_photo=True)
    factory=p.runner.search_factory
    def construct(*args):
        source=factory(*args)
        source.fetch=lambda *_:pytest.fail("expired repair spent provider call")
        expire(p)
        return source
    p.runner.search_factory=construct
    run(p,lambda *_:pytest.fail("expired owner edit"))
    with Session(p.engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==before
        assert db.get(SourceProbe,"owner-photo-repair-v1-111-124") is None
    assert_sent_unchanged(p)


def test_expiry_during_photo_read_prevents_edit_without_rewriting_sent_claim(p,monkeypatch):
    configured(p,monkeypatch)
    before=paid(p,missing_photo=True)
    factory=p.runner.search_factory
    calls=[]
    def construct(*args):
        source=factory(*args)
        fetch=source.fetch
        def get(key,path,params):
            calls.append(path)
            result=fetch(key,path,params)
            if path=="info":
                result["photoData"]={"seoLinkF":URL}
                expire(p)
            return result
        source.fetch=get
        return source
    p.runner.search_factory=construct
    run(p,lambda *_:pytest.fail("expiry during read bypassed edit access gate"))
    assert calls==["info"]
    with Session(p.engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==before+1
        assert db.get(SourceProbe,"owner-photo-repair-v1-111-124") is None
    assert_sent_unchanged(p)


def test_expiry_after_prepared_claim_is_rechecked_immediately_before_edit(p,monkeypatch):
    configured(p,monkeypatch)
    before=paid(p)
    original_commit=Session.commit
    changed=[False]
    def commit(db):
        # Simulate a committed expiry between the edit claim and final send gate.
        repair=db.get(SourceProbe,"owner-photo-repair-v1-111-124")
        if repair and repair.status=="editing" and not changed[0]:
            changed[0]=True
            db.get(Entitlement,111).expires_at=p.clock[0]
        return original_commit(db)
    monkeypatch.setattr(Session,"commit",commit)
    run(p,lambda *_:pytest.fail("prepared claim bypassed current access gate"))
    with Session(p.engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==before
        assert db.get(SourceProbe,"owner-photo-repair-v1-111-124").status=="cancelled"
    assert changed[0]
    assert_sent_unchanged(p)


def test_owner_current_access_permits_one_explicit_edit_without_paid_read(p,monkeypatch):
    configured(p,monkeypatch)
    before=paid(p)
    calls=[]
    run(p,lambda uid,mid,car:calls.append((uid,mid)) or
        {"ok":True,"result":{"message_id":mid,"photo":[{}]}})
    run(p,lambda *_:pytest.fail("already issued edit repeated"))
    assert calls==[(111,71)]
    with Session(p.engine) as db:
        assert db.get(SourceBudget,"auto_ria").total==before
    assert_sent_unchanged(p)
