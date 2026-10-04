"""Private aggregate history. SELECT only; no source calls or client messages."""
from datetime import datetime, timedelta

from sqlalchemy import case, func, select

from .billing_models import BillingCampaign, CampaignRecipient
from .manual_payment_models import PaymentRequest, ReceiptExpectation


def history(db, schedule, now):
    from .tariff_reminders import PREFIX, ZONE, result
    today = datetime.fromtimestamp(now, ZONE).date()
    dates = [today-timedelta(days=n) for n in range(6, -1, -1)]
    keys = [PREFIX+day.isoformat() for day in dates]
    campaigns = {row.id: row for row in db.scalars(select(BillingCampaign).where(BillingCampaign.id.in_(keys)))}
    receipts = dict(db.execute(select(CampaignRecipient.campaign_id, func.count()).where(
        CampaignRecipient.campaign_id.in_(keys), CampaignRecipient.state == "sent",
        CampaignRecipient.message_id.is_not(None)).group_by(CampaignRecipient.campaign_id)).all())
    records = []
    for day, key in zip(dates, keys):
        morning = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=9).timestamp()
        campaign = campaigns.get(key)
        if campaign:
            item = result(db, campaign, now)
            item.update(run_observed=True, telegram_accepted=receipts.get(key, 0),
                        sent_without_receipt=item["sent"]-receipts.get(key, 0))
        else:
            status = ("before_first_planned_run" if schedule and morning < schedule.first_run_at
                      else "not_yet_due" if morning > now else "no_saved_campaign")
            item = {"campaign_id": key, "date_kyiv": day.isoformat(), "status": status,
                    "run_observed": False, "selected": None, "queued": None,
                    "telegram_accepted": None, "excluded": None,
                    "audience_at_selection": None}
        records.append(item)
    return {"basis": "saved_daily_campaigns_and_telegram_message_ids",
            "records": records, "missing_campaign_is_not_zero_delivery": True}


def pending_payments(db, now):
    """Seven days is an audit label, never an automatic rejection or deletion."""
    cutoff = now-7*86400
    rows = db.execute(select(PaymentRequest.state, func.count(),
        func.sum(case((PaymentRequest.updated_at < cutoff, 1), else_=0)),
        func.min(PaymentRequest.updated_at)).where(
            PaymentRequest.state.in_(("created", "review", "clarification")))
        .group_by(PaymentRequest.state)).all()
    expected, stale = db.execute(select(func.count(), func.coalesce(func.sum(case(
        (ReceiptExpectation.updated_at < cutoff, 1), else_=0)), 0)).select_from(ReceiptExpectation)
        .where(ReceiptExpectation.active.is_(True))).one()
    return {"stale_after_days": 7,
            "requests": {state: {"total": count, "stale": old, "oldest_updated_at": first}
                         for state, count, old, first in rows},
            "active_receipt_expectations": expected, "stale_receipt_expectations": stale,
            "action": "report_only_no_payment_mutation"}
