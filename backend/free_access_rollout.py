"""Explicit operator transition. No network calls, purchases or quota resets."""
import math
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import billing, paid_source_access
from .billing_models import BillingCampaign, TariffReminderSchedule
from .models import Delivery, MonitorMembership, MonitorWatch, Search, SourceProbe, User
from .monitor import source_filters

ID = "free-access-rollout-20261026"


def values(row):
    return None if row is None else {c.name: getattr(row, c.name) for c in row.__table__.columns}


def activate(engine, settings, *, actor, now=None):
    if not actor or actor != settings.admin_telegram_id:
        raise ValueError("owner_required")
    if not paid_source_access.strict(engine):
        raise ValueError("production_policy_required")
    now = time.time() if now is None else now
    with Session(engine) as db, db.begin():
        if engine.dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '30s'"))
        # Match subscription/delivery lock ordering: users, then billing control.
        users = list(db.scalars(select(User).order_by(User.id).with_for_update()))
        ctrl = billing.control(db, lock=True)
        if ctrl is None:
            raise ValueError("billing_control_missing")
        previous = db.get(SourceProbe, ID)
        if previous is not None:
            if previous.status != "applied" or not billing.free_access_enabled(db):
                raise ValueError("rollout_already_recorded_review_required")
            return {**previous.result["summary"], "replayed": True}
        eligible_users = {u.id for u in users if u.ready and u.id not in paid_source_access.exclusions(engine)}
        searches = list(db.scalars(select(Search).where(Search.enabled.is_(True),
            Search.user_id.in_(eligible_users)).order_by(Search.id)))
        newly = [s for s in searches if not paid_source_access.allowed(db, s.user_id, now)]
        schedules = list(db.scalars(select(TariffReminderSchedule)))
        campaigns = list(db.scalars(select(BillingCampaign).where(
            BillingCampaign.status.in_(("prepared", "running", "paused", "blocked")))))
        newly_uids = {s.user_id for s in newly}
        pending = list(db.scalars(select(Delivery).where(Delivery.user_id.in_(newly_uids),
            Delivery.state == "pending")))
        backup = {"control": values(ctrl), "schedules": [values(s) for s in schedules],
            "campaign_statuses": {c.id: c.status for c in campaigns},
            "watches": [values(db.get(MonitorWatch, s.id)) for s in newly],
            "memberships": [values(db.get(MonitorMembership, s.id)) for s in newly],
            "pending_deliveries": [d.id for d in pending]}
        # Preserve historical seen/match rows and all sent/uncertain receipts.
        # A new epoch invalidates old pending work; start at today's switch.
        for search in newly:
            epoch = uuid.uuid4().hex
            watch = db.get(MonitorWatch, search.id)
            if watch is None:
                watch = MonitorWatch(search_id=search.id)
                db.add(watch)
            watch.epoch, watch.initialized, watch.window = epoch, False, []
            watch.status, watch.next_poll, watch.checked_at = "starting", 0, 0
            member = db.get(MonitorMembership, search.id)
            if member is None:
                member = MonitorMembership(search_id=search.id)
                db.add(member)
            member.epoch = epoch
            member.feed_id = source_filters(search.filters).fingerprint()
            member.started_at = math.floor(now)
        for row in pending:
            row.state = "cancelled"
        for row in schedules:
            row.enabled = False
        for row in campaigns:
            row.status = "cancelled"
        ctrl.sales = ctrl.enforce = False
        ctrl.offer = {**(ctrl.offer or {}), billing.FREE_ACCESS_KEY: True}
        summary = {"activated_at": now, "actor": actor, "eligible_searches": len(searches),
            "eligible_users": len({s.user_id for s in searches}),
            "newly_eligible_searches": len(newly), "cancelled_old_pending": len(pending),
            "sales_enabled": False, "tariff_reminders_enabled": False}
        db.add(SourceProbe(id=ID, status="applied", checked_at=now, requests=0,
            result={"summary": summary, "backup": backup}))
        return {**summary, "replayed": False}

