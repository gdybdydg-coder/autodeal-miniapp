"""Explicit one-shot worker. Never started by importing the API."""
import logging
import time

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .app import Settings
from .models import (Car, Delivery, DeliveryTiming, Filters, Listing, MonitorJob, MonitorMatch,
                     MonitorSeen, MonitorWatch, Search, User)
from .valuation import evidence_valid, is_deal, price_only_evidence_valid
from .peer_cache import evidence_current


def matches(car: Car, filters: Filters):
    if not is_deal(car.price, car.market, filters.minDiscount):
        return False
    for key in ("brand", "model", "region"):
        if getattr(filters, key) and getattr(filters, key) != getattr(car, key):
            return False
    for key in ("body", "fuel", "transmission"):
        value = getattr(car, key)
        if getattr(filters, key) and value and value not in getattr(filters, key):
            return False
    for key, value in (("price", car.price), ("year", car.year)):
        bounds = getattr(filters, key)
        if bounds.from_ is not None and value < bounds.from_:
            return False
        if bounds.to is not None and value > bounds.to:
            return False
    if car.mileage is not None:
        bounds, value = filters.mileage, car.mileage / 1000
        if bounds.from_ is not None and value < bounds.from_:
            return False
        if bounds.to is not None and value > bounds.to:
            return False
    return True


def fresh(car, now, db=None):
    return (-30 <= now - car.observed_at <= (300 if car.source == "auto_ria" else 86400)
            and (car.source != "auto_ria" or price_only_evidence_valid(car, now)
                 or (evidence_valid(car, now)
                     and (db is None or evidence_current(db, car.valuation_evidence)))))


def ingest(engine, cars: list[Car], now=None):
    """Trusted internal adapter only; no public endpoint and no test seed on boot."""
    now = time.time() if now is None else now
    with Session(engine) as db:
        for car in cars:
            if not fresh(car, now):
                continue
            row = db.scalar(select(Listing).where(Listing.source == car.source, Listing.source_id == car.source_id))
            if row:
                row.car = car.model_dump(mode="json")
            else:
                db.add(Listing(source=car.source, source_id=car.source_id, car=car.model_dump(mode="json")))
        db.commit()


def matching_searches(user_id, listing_id):
    return select(MonitorMatch.search_id).join(Search, Search.id == MonitorMatch.search_id).join(
        MonitorWatch, MonitorWatch.search_id == Search.id).where(
        MonitorMatch.listing_id == listing_id, Search.user_id == user_id, Search.enabled.is_(True),
        MonitorMatch.epoch == MonitorWatch.epoch, MonitorMatch.fingerprint == Search.fingerprint)


def eligible(db, user_id, listing, now):
    car = Car.model_validate(listing.car)
    if not fresh(car, now, db):
        return False
    if car.source == "auto_ria":
        # Production matches use official catalog IDs, not translated labels.
        # The trusted monitor records the filter fingerprint and enable epoch.
        price_only = price_only_evidence_valid(car, now)
        return any(price_only or is_deal(car.price, car.market, Filters.model_validate(search.filters).minDiscount)
                   for search in db.scalars(select(Search).where(
                       Search.id.in_(matching_searches(user_id, listing.id)))))
    return any(matches(car, Filters.model_validate(row.filters)) for row in db.scalars(
        select(Search).where(Search.user_id == user_id, Search.enabled.is_(True),
                             Search.after_listing < listing.id)))


def enqueue(engine, now=None):
    now = time.time() if now is None else now
    with Session(engine) as db:
        for user in db.scalars(select(User).where(User.ready.is_(True))):
            matched = select(MonitorMatch.listing_id).join(Search, Search.id == MonitorMatch.search_id).where(
                Search.user_id == user.id, Search.enabled.is_(True))
            already_queued = select(Delivery.listing_id).where(Delivery.user_id == user.id)
            for listing in db.scalars(select(Listing).where(
                    or_(Listing.source != "auto_ria", Listing.id.in_(matched)),
                    Listing.id.not_in(already_queued))):
                refreshable = (listing.source == "auto_ria"
                    and not fresh(Car.model_validate(listing.car), now, db)
                    and db.scalar(matching_searches(user.id, listing.id).limit(1)) is not None)
                if not refreshable and not eligible(db, user.id, listing, now):
                    continue
                exists = db.scalar(select(Delivery.id).where(Delivery.user_id == user.id, Delivery.listing_id == listing.id))
                if exists:
                    continue
                try:
                    with db.begin_nested():
                        delivery = Delivery(user_id=user.id, listing_id=listing.id, state="pending", retry_at=0)
                        db.add(delivery)
                        db.flush()
                        db.add(DeliveryTiming(delivery_id=delivery.id, queued_at=now))
                except IntegrityError:
                    pass  # Unique (user, source listing) is the final dedupe authority.
        db.commit()


class TelegramSender:
    def __init__(self, token):
        self.token = token
        # httpx INFO logs include request URL, which contains the bot token.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    def __call__(self, user_id, car):
        price = f"${car.price:,.0f}".replace(",", " ")
        details = []
        if car.fuel:
            details.append(f"⛽️ {car.fuel}")
        if car.transmission:
            details.append(f"⚙️ {car.transmission}")
        if car.mileage is not None:
            mileage = f"{car.mileage / 1000:g}".replace(".", ",")
            details.append(f"🛣️ Пробіг: {mileage} тис. км")
        if car.region:
            details.append(f"📍 {car.region}")
        pricing = [f"💰 Ціна: {price}"]
        if car.market is not None:
            discount = (1 - car.price / car.market) * 100
            market = f"${car.market:,.0f}".replace(",", " ")
            benefit = f"{discount:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            pricing.extend((f"📊 Ринкова ціна: ≈ {market}", f"🔥 Вигода: {benefit}%"))
        else:
            pricing.append("⚡ Неповні характеристики — перевірте оголошення")
        sections = [f"🚘 {car.brand} {car.model} · {car.year}"]
        if details:
            sections.append("\n".join(details))
        sections.extend(("\n".join(pricing), "/stop — вимкнути сповіщення"))
        text = "\n\n".join(sections)
        payload = {"chat_id": user_id, "reply_markup": {"inline_keyboard": [
            [{"text": "🔗 Відкрити оголошення", "url": str(car.url)}]]}}
        if car.photo:
            method = "sendPhoto"
            payload.update(photo=str(car.photo), caption=text[:1000])
        else:
            method = "sendMessage"
            payload.update(text=text)
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            result = response.json()
            if not isinstance(result, dict):
                return {"uncertain": True}
            return result
        except (httpx.HTTPError, ValueError):
            # A timeout may mean Telegram accepted the message. Never blindly retry.
            return {"uncertain": True}


def deliver_one(engine, settings: Settings, sender, now=None):
    if not settings.live:
        return "disabled"
    now = time.time() if now is None else now
    with Session(engine) as db:
        row = db.scalar(select(Delivery).where(
            Delivery.state == "pending", Delivery.retry_at <= now).order_by(Delivery.id).limit(1))
        if row is None:
            return "empty"
        delivery_id = row.id
        claimed = db.execute(update(Delivery).where(
            Delivery.id == delivery_id, Delivery.state == "pending").values(state="sending", retry_at=now))
        db.commit()
        if claimed.rowcount != 1:
            return "busy"
    with Session(engine) as db:
        row = db.get(Delivery, delivery_id)
        # Same lock as /stop and subscription edits; recheck immediately before send.
        user = db.scalar(select(User).where(User.id == row.user_id).with_for_update())
        listing = db.get(Listing, row.listing_id)
        refresh = []
        if user and user.ready and listing and listing.source == "auto_ria" and not fresh(Car.model_validate(listing.car), now, db):
            refresh = list(db.scalars(matching_searches(user.id, listing.id)))
        if refresh:
            # A long Telegram queue is not grounds to send an old price or silently
            # discard the opportunity. Revalidate through the normal budgeted job.
            job = db.get(MonitorJob, listing.source_id)
            if job is None:
                db.add(MonitorJob(source_id=listing.source_id, first_seen=now))
            elif job.state != "pending":
                job.state, job.next_run, job.result = "pending", 0, {}
            db.execute(update(MonitorSeen).where(MonitorSeen.search_id.in_(refresh),
                MonitorSeen.source_id == listing.source_id).values(state="pending"))
            row.state, row.retry_at = "pending", now + 5
        elif not user or not user.ready or not listing or not eligible(db, row.user_id, listing, now):
            row.state = "cancelled"
        else:
            car = Car.model_validate(listing.car)
            timing = db.get(DeliveryTiming, delivery_id)
            if timing is None:
                timing = DeliveryTiming(delivery_id=delivery_id, queued_at=now)
                db.add(timing)
            for field in ("discovered_at", "evaluated_at", "source_added_at"):
                setattr(timing, field, (car.pipeline or {}).get(field))
            timing.send_started_at = time.time()
            try:
                result = sender(row.user_id, car)
            except Exception:
                result = {"uncertain": True}
            if result.get("ok") is True and type(result.get("result", {}).get("message_id")) is int:
                row.state = "sent"
                row.message_id = result["result"]["message_id"]
                timing.accepted_at = time.time()
                stamp = result["result"].get("date")
                timing.telegram_date = stamp if type(stamp) is int and stamp > 0 else None
            elif result.get("error_code") == 429:
                retry = result.get("parameters", {}).get("retry_after", 60)
                row.retry_at = now + max(1, min(int(retry), 86400))
                row.state = "pending"
            elif result.get("error_code") == 403:
                row.state = "failed"
                user.ready = False
                db.execute(update(Search).where(Search.user_id == user.id).values(enabled=False))
                db.execute(update(Delivery).where(Delivery.user_id == user.id, Delivery.state == "pending").values(state="cancelled"))
            elif result.get("error_code") in (400, 401, 404):
                row.state = "failed"
            else:
                row.state = "uncertain"
        db.commit()
        return row.state


def main():
    from sqlalchemy import create_engine
    settings = Settings.env()
    if not settings.live:
        print("Delivery disabled; no network requests made.")
        return
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    enqueue(engine)
    sender = TelegramSender(settings.bot_token)
    # Explicit invocation handles one message; schedule deliberately after approval.
    print(deliver_one(engine, settings, sender))


if __name__ == "__main__":
    main()
