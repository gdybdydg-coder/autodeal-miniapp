"""Explicit one-shot worker. Never started by importing the API."""
import logging
import time

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .app import Settings
from .models import Car, Delivery, Filters, Listing, MonitorMatch, MonitorWatch, Search, User


def matches(car: Car, filters: Filters):
    # Alerts always require >=15% below supplied valuation, even for broad searches.
    if car.price > car.market * 0.85:
        return False
    for key in ("brand", "model", "region"):
        if getattr(filters, key) and getattr(filters, key) != getattr(car, key):
            return False
    for key in ("body", "fuel", "transmission"):
        if getattr(filters, key) and getattr(car, key) not in getattr(filters, key):
            return False
    for key, value in (("price", car.price), ("year", car.year), ("mileage", car.mileage / 1000)):
        bounds = getattr(filters, key)
        if bounds.from_ is not None and value < bounds.from_:
            return False
        if bounds.to is not None and value > bounds.to:
            return False
    return True


def fresh(car, now):
    return -30 <= now - car.observed_at <= (300 if car.source == "auto_ria" else 86400)


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


def eligible(db, user_id, listing, now):
    car = Car.model_validate(listing.car)
    if not fresh(car, now):
        return False
    if car.source == "auto_ria":
        if car.price > car.market * .85:
            return False
        # Production matches use official catalog IDs, not translated labels.
        # The trusted monitor records the filter fingerprint and enable epoch.
        return db.scalar(select(MonitorMatch.search_id).join(Search, Search.id == MonitorMatch.search_id)
            .join(MonitorWatch, MonitorWatch.search_id == Search.id).where(
                MonitorMatch.listing_id == listing.id, Search.user_id == user_id,
                Search.enabled.is_(True), MonitorMatch.epoch == MonitorWatch.epoch,
                MonitorMatch.fingerprint == Search.fingerprint).limit(1)) is not None
    return any(matches(car, Filters.model_validate(row.filters)) for row in db.scalars(
        select(Search).where(Search.user_id == user_id, Search.enabled.is_(True),
                             Search.after_listing < listing.id)))


def enqueue(engine, now=None):
    now = time.time() if now is None else now
    with Session(engine) as db:
        for user in db.scalars(select(User).where(User.ready.is_(True))):
            for listing in db.scalars(select(Listing)):
                if not eligible(db, user.id, listing, now):
                    continue
                exists = db.scalar(select(Delivery.id).where(Delivery.user_id == user.id, Delivery.listing_id == listing.id))
                if exists:
                    continue
                try:
                    with db.begin_nested():
                        db.add(Delivery(user_id=user.id, listing_id=listing.id, state="pending", retry_at=0))
                        db.flush()
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
        discount = (1 - car.price / car.market) * 100
        text = (
            f"Вигідне авто: {car.brand} {car.model}\n"
            f"{car.year} · {car.fuel} · {car.mileage / 1000:g} тис. км\n"
            f"{car.transmission} · {car.region}\n"
            f"Ціна: ${car.price:,.0f}\n"
            f"Медіана {car.comparables} схожих оголошень: ${car.market:,.0f} · нижче на {discount:.1f}%\n"
            "Це оцінка, не гарантія стану або вигоди. Перевір авто перед купівлею.\n"
            "/stop — вимкнути всі сповіщення"
        )
        payload = {"chat_id": user_id, "reply_markup": {"inline_keyboard": [
            [{"text": "Відкрити оголошення", "url": str(car.url)}]]}}
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
        if not user or not user.ready or not listing or not eligible(db, row.user_id, listing, now):
            row.state = "cancelled"
        else:
            try:
                result = sender(row.user_id, Car.model_validate(listing.car))
            except Exception:
                result = {"uncertain": True}
            if result.get("ok") is True and type(result.get("result", {}).get("message_id")) is int:
                row.state = "sent"
                row.message_id = result["result"]["message_id"]
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
