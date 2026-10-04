"""Explicit one-shot worker. Never started by importing the API."""
from . import billing, paid_source_access
import json
import logging
import time

import httpx
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .app import Settings
from .models import (Car, Delivery, DeliveryTiming, Filters, Listing, MonitorJob, MonitorMatch,
                     MonitorSeen, MonitorWatch, Search, User)
from .valuation import evidence_valid, is_deal, price_only_evidence_valid
from .peer_cache import evidence_current
from .reference_valuation import VERSION as REFERENCE_VERSION
from . import ria_market_range
from . import delivery_diagnostic
from .telegram_photo import PhotoLoader

delivery_log = logging.getLogger("autodeal.delivery")
delivery_log.setLevel(logging.INFO)
delivery_log.propagate = False
if not delivery_log.handlers:
    delivery_log.addHandler(logging.StreamHandler())


def matches(car: Car, filters: Filters):
    if not is_deal(car.price, car.market, filters.minDiscount):
        return False
    for key in ("brand", "model"):
        if getattr(filters, key) and getattr(filters, key) != getattr(car, key):
            return False
    if filters.regions and car.region not in filters.regions:
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


def fresh(car, now, db=None, *, require_provider_range=False):
    if require_provider_range and car.source == "auto_ria":
        proof = car.valuation_evidence or {}
        if car.market is None:
            if proof.get("pricing_policy") != ria_market_range.VERSION:
                return False
        elif (proof.get("version") != ria_market_range.VERSION
              or not ria_market_range.range_valid(proof.get("source_range"), car.source_id, now)):
            return False
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


def eligible(db, user_id, listing, now, *, require_provider_range=False, require_confirmed_deal=False):
    car = Car.model_validate(listing.car)
    if require_confirmed_deal and car.source == "auto_ria" and car.market is None:
        return False
    if not fresh(car, now, db, require_provider_range=require_provider_range):
        return False
    if car.source == "auto_ria":
        # Production matches use official catalog IDs, not translated labels.
        # The trusted monitor records the filter fingerprint and enable epoch.
        price_only = not require_confirmed_deal and price_only_evidence_valid(car, now)
        return any(price_only or is_deal(car.price, car.market, Filters.model_validate(search.filters).minDiscount)
                   for search in db.scalars(select(Search).where(
                       Search.id.in_(matching_searches(user_id, listing.id)))))
    return any(matches(car, Filters.model_validate(row.filters)) for row in db.scalars(
        select(Search).where(Search.user_id == user_id, Search.enabled.is_(True),
                             Search.after_listing < listing.id)))


def supplemental_listing(listing):
    return bool(listing and listing.source == "auto_ria"
                and (listing.car.get("pipeline") or {}).get("discovery_kind") == "active_window")


def unpriced_listing(listing):
    return bool(listing and listing.source == "auto_ria" and listing.car.get("market") is None)


def recent_listing(listing, now):
    if not listing or listing.source != "auto_ria":
        return False, False
    pipeline = listing.car.get("pipeline") or {}
    recent = pipeline.get("discovery_kind") == "html_new_publication"
    expires = pipeline.get("html_expires_at")
    return recent, recent and (type(expires) not in (int, float) or expires < now)


def enqueue(engine, now=None, *, allow_active_window=True, allow_recent_publications=True,
            require_provider_range=False, require_confirmed_deal=False):
    from .owner_car_notifications import reclaim, reusable_clause, retire_if_due
    now = time.time() if now is None else now
    retire_if_due(engine, None)
    with Session(engine) as db:
        # Drive fan-out from current, enabled monitor interests instead of
        # rescanning the entire listings table once per connected user.
        pending_pair = (~exists(select(Delivery.id).where(
            Delivery.user_id == User.id, Delivery.listing_id == Listing.id))
            | exists(select(Delivery.id).where(Delivery.user_id == User.id,
                Delivery.listing_id == Listing.id, *reusable_clause())))
        pairs = db.execute(select(User.id, Listing.id).select_from(MonitorMatch)
            .join(Search, Search.id == MonitorMatch.search_id)
            .join(MonitorWatch, MonitorWatch.search_id == Search.id)
            .join(User, User.id == Search.user_id)
            .join(Listing, Listing.id == MonitorMatch.listing_id)
            .where(User.ready.is_(True), Search.enabled.is_(True),
                   MonitorMatch.epoch == MonitorWatch.epoch,
                   MonitorMatch.fingerprint == Search.fingerprint,
                   Listing.source == "auto_ria", pending_pair)
            .distinct().order_by(Listing.id.desc(), User.id)).all()
        # Trusted non-AUTO.RIA fixture records retain their activation rule.
        other_listings = list(db.scalars(select(Listing).where(Listing.source != "auto_ria")))
        if other_listings:
            for user in db.scalars(select(User).where(User.ready.is_(True))):
                pairs.extend((user.id, listing.id) for listing in other_listings
                             if db.scalar(select(Delivery.id).where(
                                 Delivery.user_id == user.id,
                                 Delivery.listing_id == listing.id)) is None)
        for uid, listing_id in pairs:
            if not paid_source_access.allowed(db, uid, now):
                continue
            listing = db.get(Listing, listing_id)
            if not allow_active_window and supplemental_listing(listing):
                continue
            recent, expired = recent_listing(listing, now)
            if expired or (recent and not allow_recent_publications):
                continue
            # Do this before stale-price refresh: legacy informational cards
            # must not be queued or revived by enabling confirmed-only mode.
            if require_confirmed_deal and unpriced_listing(listing):
                continue
            existing = db.scalar(select(Delivery).where(
                Delivery.user_id == uid, Delivery.listing_id == listing_id))
            if existing is not None:
                # Only a fresh current match may reclaim a never-attempted copy
                # reservation. This cannot refresh or replay old copy history.
                if eligible(db, uid, listing, now, require_provider_range=require_provider_range,
                            require_confirmed_deal=require_confirmed_deal):
                    reclaim(db, existing, now)
                continue
            refreshable = (listing.source == "auto_ria"
                and not fresh(Car.model_validate(listing.car), now, db, require_provider_range=require_provider_range)
                and db.scalar(matching_searches(uid, listing.id).limit(1)) is not None)
            if not refreshable and not eligible(db, uid, listing, now,
                    require_provider_range=require_provider_range, require_confirmed_deal=require_confirmed_deal):
                continue
            try:
                with db.begin_nested():
                    delivery = Delivery(user_id=uid, listing_id=listing.id, state="pending", retry_at=0)
                    db.add(delivery)
                    db.flush()
                    db.add(DeliveryTiming(delivery_id=delivery.id, queued_at=now))
            except IntegrityError:
                pass  # Unique (user, source listing) is the final dedupe authority.
        db.commit()


class TelegramSender:
    def __init__(self, token):
        self.token = token
        self.photos = PhotoLoader()
        # httpx INFO logs include request URL, which contains the bot token.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    @staticmethod
    def card(car, *, historical_at=None):
        proof = car.valuation_evidence or {}
        if (car.source == "auto_ria" and car.market is not None
                and (proof.get("version") == "autoria-lower-bound-v1"
                     or proof.get("basis") == "auto_ria_ai_market_range"
                     or isinstance(proof.get("source_range"), dict)
                     and proof["source_range"].get("basis") == "auto_ria_ai_market_range")):
            raise ValueError("invalid_provider_range_evidence")
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
        provider_range = (car.valuation_evidence or {}).get("version") == ria_market_range.VERSION
        if car.market is not None and provider_range:
            pricing.extend(ria_market_range.pricing_lines(car, historical_at=historical_at))
            if (not (car.valuation_evidence or {}).get("condition_notices")
                    and (car.valuation_evidence or {}).get("candidate", {}).get("comparable_condition") is not True):
                pricing.append("⚠️ Стан авто не підтверджено даними джерела")
        elif car.market is not None:
            discount = (1 - car.price / car.market) * 100
            market = f"${car.market:,.0f}".replace(",", " ")
            benefit = f"{discount:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            proof = car.valuation_evidence or {}
            reference = proof.get("version") == REFERENCE_VERSION
            pricing.extend((f"📊 Обережний ціновий орієнтир: ≈ {market}",
                            f"📉 Нижче орієнтира: {benefit}%",
                            f"ℹ️ Нижній квартиль цін {car.comparables} схожих авто",
                            "Ціни оголошень; фактична ціна продажу невідома"))
            if reference:
                pricing.append("Приблизний орієнтир: великий розкид цін аналогів"
                               if proof.get("reference_kind") == "lower_price_band"
                               else "Приблизна оцінка за ширшим порівнянням")
                if (car.valuation_evidence or {}).get("unknown_dimensions"):
                    pricing.append("Частину характеристик не вказано — оцінка приблизна")
                if (not proof.get("condition_notices")
                        and proof.get("candidate", {}).get("comparable_condition") is not True):
                    pricing.append("⚠️ Стан авто не підтверджено даними джерела")
        else:
            pricing.append("ℹ️ Ринкову оцінку не підтверджено — це не підтверджена вигода")
            reasons = (car.valuation_evidence or {}).get("uncertainty_reasons", [])
            if "native_market_range_unverified" in reasons:
                pricing.append("Діапазон оцінки цього авто в AUTO.RIA не підтверджено")
            elif "provider_market_range_unavailable" in reasons:
                pricing.append("AUTO.RIA не надала придатний діапазон оцінки")
            if "incomplete_details" in reasons:
                pricing.append("Неповні характеристики — перевірте оголошення")
            if "insufficient_comparables" in reasons:
                pricing.append("Недостатньо зіставних авто для оцінки")
            if "mixed_sample" in reasons:
                pricing.append("Ціни аналогів надто різняться; нижній орієнтир не підтверджено")
            if "unverified_condition" in reasons and not (car.valuation_evidence or {}).get("condition_notices"):
                pricing.append("⚠️ Стан авто не підтверджено даними джерела")
        condition_notices = (car.valuation_evidence or {}).get("condition_notices") or []
        if condition_notices:
            if "onRepairParts" in condition_notices:
                pricing.append("⚠️ AUTO.RIA: авто на запчастини / під ремонт — перевір опис")
            if any(flag in condition_notices for flag in ("damage", "technical_condition")):
                pricing.append("⚠️ У джерелі є позначка про пошкодження / ремонт — перевір опис")
            if car.market is not None:
                pricing.append("Витрати на ремонт не враховані" if provider_range else
                               "Орієнтир аналогів без позначених пошкоджень; витрати на ремонт не враховані")
        sections = [f"🚘 {car.brand} {car.model} · {car.year}"]
        if (car.pipeline or {}).get("discovery_kind") == "active_window":
            sections.append("🕘 Активне оголошення з додаткової перевірки")
        if details:
            sections.append("\n".join(details))
        sections.extend(("\n".join(pricing), "/stop — вимкнути сповіщення"))
        text = "\n\n".join(sections)
        return text, {"inline_keyboard": [
            [{"text": "🔗 Відкрити оголошення", "url": str(car.url)}]]}

    def __call__(self, user_id, car, *, before_transport=None):
        text, markup = self.card(car)
        payload = {"chat_id": user_id, "reply_markup": markup}
        photo = self.photos.load(str(car.photo)) if car.photo else None
        transport = "upload" if photo else "url" if car.photo else "none"
        if car.photo:
            method = "sendPhoto"
            payload.update(photo=str(car.photo), caption=text[:1000])
        else:
            method = "sendMessage"
            payload.update(text=text)
        attempts = []
        def blocked():
            if before_transport is None:
                return None
            try:
                allowed = before_transport()
            except Exception:
                denial = {"ok": False, "_access_unavailable": True}
                return {**denial, "_delivery_attempts": [*attempts,
                        delivery_diagnostic.summary(denial, method)]}
            if not allowed:
                denial = {"ok": False, "_access_blocked": True}
                return {**denial, "_delivery_attempts": [*attempts,
                        delivery_diagnostic.summary(denial, method)]}
            return None
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                denial = blocked()
                if denial:
                    return denial
                if photo:
                    form = {"chat_id": str(user_id), "caption": payload["caption"],
                            "reply_markup": json.dumps(markup, ensure_ascii=False)}
                    response = client.post(f"https://api.telegram.org/bot{self.token}/{method}",
                        data=form, files={"photo": ("car.jpg", photo, "image/jpeg")})
                else:
                    response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
                result = response.json()
                if response.status_code >= 500:
                    result = {"uncertain": True}
                # Only an explicit negative Bot API response permits fallback.
                # Timeout, malformed JSON, 5xx and ambiguous success never do.
                if (method == "sendPhoto" and isinstance(result, dict)
                        and result.get("ok") is False and result.get("error_code") == 400
                        and response.status_code < 500):
                    attempts.append(delivery_diagnostic.summary(result, method))
                    method = "sendMessage"
                    plain = {"chat_id": user_id, "reply_markup": payload["reply_markup"],
                             "text": text[:4096], "link_preview_options": {"is_disabled": True}}
                    denial = blocked()
                    if denial:
                        return denial
                    response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=plain)
                    result = response.json()
                    if response.status_code >= 500:
                        result = {"uncertain": True}
            if not isinstance(result, dict):
                result = {"uncertain": True}
        except (httpx.HTTPError, ValueError):
            # A timeout may mean Telegram accepted the message. Never blindly retry.
            result = {"uncertain": True}
        attempts.append(delivery_diagnostic.summary(result, method))
        return {**result, "_delivery_attempts": attempts, "_photo_transport": transport}

    def add_photo(self, user_id, message_id, car, *, historical_at=None):
        """Edit one existing text card in place; never sends a new message."""
        photo = self.photos.load(str(car.photo)) if car.photo else None
        if not photo:
            return {"ok": False, "photo_unavailable": True}
        text, markup = self.card(car, historical_at=historical_at)
        form = {"chat_id": str(user_id), "message_id": str(message_id),
                "media": json.dumps({"type": "photo", "media": "attach://photo", "caption": text[:1000]}, ensure_ascii=False),
                "reply_markup": json.dumps(markup, ensure_ascii=False)}
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post(f"https://api.telegram.org/bot{self.token}/editMessageMedia",
                    data=form, files={"photo": ("car.jpg", photo, "image/jpeg")})
                result = response.json()
                return result if response.status_code < 500 and isinstance(result, dict) else {"uncertain": True}
        except (httpx.HTTPError, ValueError):
            return {"uncertain": True}


def deliver_one(engine, settings: Settings, sender, now=None, *, enforce_chat_interval=True):
    from .owner_car_notifications import ordinary_delivery_clause
    if not settings.live:
        return "disabled"
    # Explicit timestamps belong to isolated deterministic callers. Production
    # uses a fresh clock after locks and at every actual transport boundary.
    clock = time.time if now is None else lambda fixed=now: fixed
    now = clock()
    with Session(engine) as db:
        row = db.scalar(select(Delivery).where(
            Delivery.state == "pending", Delivery.retry_at <= now,
            ordinary_delivery_clause()).order_by(Delivery.id)
            .with_for_update(skip_locked=True).limit(1))
        if row is None:
            return "empty"
        delivery_id = row.id
        claimed_state = row.state
        claimed = db.execute(update(Delivery).where(
            Delivery.id == delivery_id, Delivery.state == claimed_state,
            Delivery.retry_at <= now).values(state="sending", retry_at=now))
        db.commit()
        if claimed.rowcount != 1:
            return "busy"
    with Session(engine) as db:
        row = db.get(Delivery, delivery_id)
        # Same lock as /stop and subscription edits; recheck immediately before send.
        user = db.scalar(select(User).where(User.id == row.user_id).with_for_update())
        now = clock()  # A row-lock wait can outlast the paid period.
        listing = db.get(Listing, row.listing_id)
        last_send = db.scalar(select(func.max(DeliveryTiming.send_started_at))
            .join(Delivery, Delivery.id == DeliveryTiming.delivery_id)
            .where(Delivery.user_id == row.user_id, Delivery.id != delivery_id)) if user else None
        refresh = []
        retired = not settings.ria_active_window_enabled and supplemental_listing(listing)
        recent, expired = recent_listing(listing, now)
        retired |= expired or (recent and not getattr(settings, "ria_recent_publications_enabled", False))
        unconfirmed = settings.ria_confirmed_deals_only and unpriced_listing(listing)
        if not retired and not unconfirmed and user and user.ready and listing and listing.source == "auto_ria" and not fresh(
                Car.model_validate(listing.car), now, db, require_provider_range=settings.ria_ai_price_enabled):
            refresh = list(db.scalars(matching_searches(user.id, listing.id)))
        try:
            paid = not user or paid_source_access.allowed(db, user.id, now)
        except Exception:
            # PostgreSQL read errors can abort the transaction. Roll back before
            # returning the claim to the queue and recording its technical cause.
            car = Car.model_validate(listing.car)
            db.rollback()
            row = db.scalar(select(Delivery).where(Delivery.id == delivery_id,
                Delivery.state == "sending").with_for_update())
            if row is None:
                return "busy"
            row.state, row.retry_at = "pending", clock() + 60
            delivery_diagnostic.record(db, row, car, {"_access_unavailable": True}, delivery_log)
            db.commit()
            return row.state
        if user and not paid:
            row.state = "cancelled"
            db.execute(update(Delivery).where(Delivery.user_id == user.id,
                Delivery.state == "pending").values(state="cancelled"))
        elif retired or unconfirmed:
            row.state = "cancelled"
            if unconfirmed:
                delivery_log.info("Unconfirmed notification suppressed source_id=%s", listing.source_id)
        elif refresh:
            # A long Telegram queue is not grounds to send an old price or silently
            # discard the opportunity. Revalidate through the normal budgeted job.
            job = db.get(MonitorJob, listing.source_id)
            origin = (listing.car.get("pipeline") or {}).get("discovery_kind", "new_publication")
            if job is not None and job.state == "manual_review":
                # A stale queue cannot override a provider-endpoint hold. Keep
                # the card/evidence until explicit operator resolution.
                row.state, row.retry_at = "pending", now + 60
            elif job is None:
                db.add(MonitorJob(source_id=listing.source_id, first_seen=now,
                                  result={"discovery_kind": origin}))
            elif job.state != "pending":
                origin = job.result.get("discovery_kind") or origin
                job.state, job.next_run = "pending", 0
                from .shared_distribution import proof
                job.result = {"discovery_kind": origin, **proof(job.result)}
            if job is None or job.state != "manual_review":
                db.execute(update(MonitorSeen).where(MonitorSeen.search_id.in_(refresh),
                    MonitorSeen.source_id == listing.source_id).values(state="pending"))
                row.state, row.retry_at = "pending", now + 5
        elif enforce_chat_interval and user and user.ready and last_send and now < last_send + 1.05:
            # Telegram limits each private chat separately; do not keep a user
            # row locked while waiting or jeopardize other recipients' slots.
            row.state, row.retry_at = "pending", last_send + 1.05
        elif not user or not user.ready or not listing or not eligible(
                db, row.user_id, listing, now, require_provider_range=settings.ria_ai_price_enabled,
                require_confirmed_deal=settings.ria_confirmed_deals_only):
            row.state = "cancelled"
        else:
            car = Car.model_validate(listing.car)
            timing = db.get(DeliveryTiming, delivery_id)
            if timing is None:
                timing = DeliveryTiming(delivery_id=delivery_id, queued_at=now)
                db.add(timing)
            for field in ("discovered_at", "evaluated_at", "source_added_at"):
                setattr(timing, field, (car.pipeline or {}).get(field))
            transport_started = False
            def before_transport():
                nonlocal transport_started
                checked_at = clock()
                if not db.scalar(select(User.ready).where(User.id == row.user_id)):
                    db.execute(update(Delivery).where(Delivery.user_id == row.user_id,
                        Delivery.state == "pending").values(state="cancelled"))
                    return False
                if not paid_source_access.allowed(db, row.user_id, checked_at):
                    db.execute(update(Delivery).where(Delivery.user_id == row.user_id,
                        Delivery.state == "pending").values(state="cancelled"))
                    return False
                if recent_listing(listing, checked_at)[1] or not eligible(
                        db, row.user_id, listing, checked_at,
                        require_provider_range=settings.ria_ai_price_enabled,
                        require_confirmed_deal=settings.ria_confirmed_deals_only):
                    return False
                if not transport_started:
                    timing.send_started_at = checked_at
                    transport_started = True
                return True
            try:
                if isinstance(sender, TelegramSender):
                    result = sender(row.user_id, car, before_transport=before_transport)
                elif before_transport():
                    result = sender(row.user_id, car)
                else:
                    result = {"ok": False, "_access_blocked": True}
            except Exception:
                result = {"uncertain": True} if transport_started else {"_access_unavailable": True}
            if not isinstance(result, dict):
                result = {"uncertain": True}
            message = result.get("result")
            message = message if isinstance(message, dict) else {}
            if result.get("ok") is True and type(message.get("message_id")) is int:
                row.state = "sent"
                row.message_id = message["message_id"]
                timing.accepted_at = time.time()
                stamp = message.get("date")
                timing.telegram_date = stamp if type(stamp) is int and stamp > 0 else None
                if car.source == "auto_ria" and car.market is not None:
                    proof = car.valuation_evidence or {}
                    if proof.get("version") == ria_market_range.VERSION:
                        quote = proof["source_range"]
                        provider = quote.get("provider") or {}
                        delivery_log.info("AUTO.RIA-price notification accepted source_id=%s average_usd=%s range_fraction=%s lower_usd=%s market_usd=%s price_usd=%s",
                            car.source_id, provider.get("average_usd"), provider.get("range_fraction"), quote["lower_usd"], car.market, car.price)
                    else:
                        delivery_log.info("Conservative-price notification accepted source_id=%s version=%s market_usd=%s median_usd=%s comparables=%s peer_prices_usd=%s",
                            car.source_id, proof.get("version"), car.market, proof.get("median_usd"),
                            car.comparables, sorted(peer["price_usd"] for peer in proof.get("peers", [])))
                elif car.source == "auto_ria":
                    job = db.get(MonitorJob, car.source_id)
                    rating = (job.result or {}).get("rating", {}) if job else {}
                    proof = rating.get("valuation_evidence") or {}
                    delivery_log.info("Unpriced notification accepted source_id=%s reasons=%s comparables=%s peer_prices_usd=%s",
                                      car.source_id, rating.get("valuation_reasons", []), rating.get("comparables", 0),
                                      sorted(peer["price_usd"] for peer in proof.get("peers", [])))
            elif result.get("_access_blocked"):
                row.state = "cancelled"
            elif result.get("_access_unavailable"):
                # A known pre-transport read failure is not Telegram acceptance.
                db.rollback()
                row = db.scalar(select(Delivery).where(Delivery.id == delivery_id,
                    Delivery.state == "sending").with_for_update())
                if row is None:
                    return "busy"
                row.state, row.retry_at = "pending", clock() + 60
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
            delivery_diagnostic.record(db, row, car, result, delivery_log)
        db.commit()
        return row.state


def main():
    from sqlalchemy import create_engine
    settings = Settings.env()
    if not settings.live:
        print("Delivery disabled; no network requests made.")
        return
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    paid_source_access.configure(engine, settings)
    from . import owner_car_notifications, paid_owner_restoration
    owner_car_notifications.initialize(engine, settings)
    paid_owner_restoration.initialize(engine, settings)
    enqueue(engine, allow_active_window=settings.ria_active_window_enabled,
            require_provider_range=settings.ria_ai_price_enabled,
            require_confirmed_deal=settings.ria_confirmed_deals_only)
    sender = TelegramSender(settings.bot_token)
    # Explicit invocation handles one message; schedule deliberately after approval.
    print(deliver_one(engine, settings, sender))


if __name__ == "__main__":
    main()
