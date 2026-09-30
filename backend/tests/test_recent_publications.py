"""Dated public intake must not resurrect old listings or bypass delivery rules."""
import asyncio
from dataclasses import replace
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import recent_publications as rp, html_shadow
from backend.auto_ria import RiaError
from backend.models import (Delivery, Listing, MonitorJob, MonitorSeen, MonitorWatch, Range,
                            Search, SourceBudget, SourceProbe, User)
from backend.monitor import Monitor, delivery_batch, reset_watch
from backend.tests.test_monitor import p, add_search, drain, wake, details, searches
from backend.tests.test_ria_ai_price import enable


def date(at):
    return datetime.fromtimestamp(at, rp.KYIV).strftime("%Y-%m-%d %H:%M:%S")


def card(sid, at, *, price=10000, paid=False, updated=None, link=None):
    return (f'<section class="ticket-item {"paid" if paid else ""}" data-advertisement-id="{sid}">'
            f'<a class="m-link-ticket" href="{link or "https://auto.ria.com/uk/auto_test_" + sid + ".html"}"></a>'
            f'<div class="price-ticket" data-main-currency="USD" data-main-price="{price}"></div>'
            f'<span data-add-date="{date(at)}" data-update-date="{date(updated or at)}"></span></section>')


def fetcher(body, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        value = "User-agent: *\nAllow: /\n" if url == html_shadow.ROBOTS else body
        return value, len(value.encode())
    return fetch


def setup(p, monkeypatch, **kwargs):
    quotes = enable(p, monkeypatch, **kwargs)
    p.settings = replace(p.runner.settings, ria_recent_publications_enabled=True,
                         ria_shared_distribution_enabled=True, ria_confirmed_deals_only=True)
    p.runner.settings = p.settings
    drain(p)
    rp.collect(p.engine, p.settings, fetcher(card("123", p.clock[0] - 1000)))
    return quotes


def offer(p, sid="77", *, changes=None, mutate=None, at=None):
    p.clock[0] += rp.interval(p.clock[0]) + 1
    at = p.clock[0] - 1 if at is None else at
    body = card(sid, at)
    factory = p.runner.search_factory
    def source_factory(engine, key):
        source = factory(engine, key)
        fetch = source.fetch
        def get(key, path, params):
            raw = fetch(key, path, params)
            if path == "info" and params["auto_id"] == sid:
                raw["addDate"] = date(at)
                raw["autoData"]["categoryId"] = 1
                if changes:
                    raw.update(changes)
                if mutate:
                    mutate(raw)
            return raw
        source.fetch = get
        return source
    p.runner.search_factory = source_factory
    rp.collect(p.engine, p.settings, fetcher(body))
    return at


def dispatch(p, count=8):
    for _ in range(count):
        asyncio.run(delivery_batch(p.engine, p.settings, p.runner.sender))


def probe(p):
    with Session(p.engine) as db:
        return dict(db.get(SourceProbe, rp.PROBE_ID).result)


def test_parser_retains_add_date_and_omits_promotions_new_cars_and_unknown_dates():
    at = 1790788800
    body = card("1", at, updated=at + 600) + card("2", at, paid=True) + card(
        "3", at, link="https://auto.ria.com/uk/newauto/auto-test-3.html")
    body += card("4", at).replace("data-add-date", "ignored-date")
    rows = rp.parse(body)
    assert rows == [{"id": "1", "added_at": at, "preview_usd": 10000}]
    assert rp.parse(card("5", at, price=0)) == []
    assert rp.parse(card("6", at).replace('data-main-currency="USD"', 'data-main-currency="UAH"'))[0]["preview_usd"] is None


@pytest.mark.parametrize("body", ["captcha", '<section class="ticket-item" data-advertisement-id="1">',
                                card("1", 1790788800) * 2])
def test_parser_fails_closed_on_changed_or_duplicate_cards(body):
    with pytest.raises(html_shadow.ProbeError):
        rp.parse(body)


@pytest.mark.parametrize("value", ["2026-10-25 03:30:00", "2026-03-29 03:30:00", "today", "2026-09-30"])
def test_ambiguous_nonexistent_or_unlabelled_dates_are_not_publication_evidence(value):
    assert rp.added_at(value) is None


def test_initial_snapshot_restart_and_price_or_update_changes_create_no_work(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    baseline = probe(p)["baseline_at"]
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    rp.collect(p.engine, p.settings, fetcher(card("123", baseline - 1000, price=100, updated=p.clock[0]) +
        card("76", baseline - 1, updated=p.clock[0]) + card("78", p.clock[0] - 1, paid=True)))
    before = len(p.calls)
    drain(p)
    assert not probe(p)["pending"] and not p.sent and not quotes
    assert len(p.calls) - before <= 1  # The ordinary due publication search only.
    assert not details(p, "123") and not details(p, "76")


def test_fresh_republication_under_old_id_shares_details_quote_and_thresholds(p, monkeypatch):
    p.clock[0] -= 2
    add_search(p, sid=2, uid=222, minDiscount=20)
    add_search(p, sid=3, uid=333, minDiscount=30)
    p.clock[0] += 2
    quotes = setup(p, monkeypatch)
    before = len(searches(p))
    offer(p, sid="77")
    drain(p)
    dispatch(p)
    assert sorted((uid, car.source_id) for uid, car in p.sent) == [(111, "77"), (222, "77")]
    assert len(details(p, "77")) == 1 and quotes == ["77"]
    assert len(searches(p)) - before == 1  # No paid fallback search for any subscriber.
    assert len(probe(p)["api"]) == 2
    assert probe(p)["validated"] == 1
    assert all(car.market == 13537.5 for _, car in p.sent)
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    rp.collect(p.engine, p.settings, fetcher(card("77", p.clock[0] - 62)))
    drain(p)
    dispatch(p)
    assert len(p.sent) == 2 and len(details(p, "77")) == 1 and quotes == ["77"]


@pytest.mark.parametrize("kind", ["old_api_date", "other_category", "no_category", "sold", "zero_price",
                                "wrong_fuel", "wrong_region", "wrong_price", "abroad", "custom"])
def test_official_details_reject_unverified_publication_and_filter_mismatches(p, monkeypatch, kind):
    quotes = setup(p, monkeypatch)
    def mutate(raw):
        if kind == "old_api_date": raw["addDate"] = date(p.clock[0] - 86400)
        elif kind == "other_category": raw["autoData"]["categoryId"] = 2
        elif kind == "no_category": raw["autoData"].pop("categoryId")
        elif kind == "sold": raw["autoData"]["isSold"] = True
        elif kind == "zero_price": raw["USD"] = 0
        elif kind == "wrong_fuel": raw["autoData"]["fuelId"] = 1
        elif kind == "wrong_region": raw["stateData"]["stateId"] = 1
        elif kind == "wrong_price": raw["USD"] = 40000
        else: raw["autoInfoBar"] = {kind: True}
    if kind == "wrong_price":
        with Session(p.engine) as db:
            search = db.get(Search, 1)
            search.filters = {**search.filters, "price": {"from": 0, "to": 20000}}
            db.commit()
    offer(p, mutate=mutate)
    drain(p)
    dispatch(p)
    assert not p.sent and not quotes
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77") is None
    assert probe(p)["discarded"] == 1


def test_missing_optional_details_and_damage_do_not_hide_a_matching_deal(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    def mutate(raw):
        for key in ("fuelId", "fuelName", "gearBoxId", "gearboxName", "bodyId", "raceInt"):
            raw["autoData"].pop(key, None)
        raw["technicalCondition"] = {"id": 3}
        raw["autoInfoBar"] = {"damage": True, "onRepairParts": True}
    offer(p, mutate=mutate)
    drain(p)
    assert quotes == ["77"] and [(uid, car.source_id) for uid, car in p.sent] == [(111, "77")]


def test_no_confirmed_ai_range_means_no_information_card(p, monkeypatch):
    setup(p, monkeypatch, response={})
    offer(p)
    drain(p)
    dispatch(p)
    assert not p.sent
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").state == "unvalued"
        assert db.scalar(select(Delivery)) is None


def test_stop_during_detail_validation_cannot_add_an_interest(p, monkeypatch):
    setup(p, monkeypatch)
    def stop(raw):
        with Session(p.engine) as db:
            db.get(User, 111).ready = False
            db.get(Search, 1).enabled = False
            reset_watch(db, 1, False)
            db.commit()
    offer(p, mutate=stop)
    drain(p)
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77") is None
        assert db.get(MonitorWatch, 1) is None
    assert not p.sent


def test_stop_during_quote_preserves_other_recipient(p, monkeypatch):
    add_search(p)
    def stop():
        with Session(p.engine) as db:
            db.get(User, 111).ready = False
            db.get(Search, 1).enabled = False
            reset_watch(db, 1, False)
            db.commit()
    quotes = setup(p, monkeypatch, action=stop)
    offer(p)
    drain(p)
    dispatch(p)
    assert quotes == ["77"] and [(uid, car.source_id) for uid, car in p.sent] == [(222, "77")]


def test_pending_candidate_survives_restart_but_late_activation_gets_no_old_ad(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    offer(p)
    add_search(p)  # This member starts after the dated publication.
    p.runner = Monitor(p.engine, p.settings, p.runner.search_factory, p.runner.sender)
    drain(p)
    dispatch(p)
    assert quotes == ["77"] and [(uid, car.source_id) for uid, car in p.sent] == [(111, "77")]


@pytest.mark.parametrize("state", ["sent", "uncertain"])
def test_existing_delivery_claim_without_job_prevents_refetch(p, monkeypatch, state):
    setup(p, monkeypatch)
    with Session(p.engine) as db:
        listing = Listing(source="auto_ria", source_id="77", car={})
        db.add(listing)
        db.flush()
        db.add(Delivery(user_id=111, listing_id=listing.id, state=state))
        db.commit()
    offer(p)
    drain(p)
    assert not details(p, "77") and not p.sent
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == state


def test_existing_primary_job_is_not_revalued_or_replaced(p, monkeypatch):
    setup(p, monkeypatch)
    with Session(p.engine) as db:
        db.add(MonitorJob(source_id="77", first_seen=p.clock[0] - 1000, state="checked",
                         result={"discovery_kind": "new_publication", "marker": "keep"}))
        db.commit()
    offer(p)
    drain(p)
    assert not details(p, "77")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").result["marker"] == "keep"


def test_http_reservation_prevents_overlapping_collectors_and_survives_failure(p, monkeypatch):
    setup(p, monkeypatch)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    entered, release, calls = threading.Event(), threading.Event(), []
    def slow(url):
        calls.append(url)
        if url == html_shadow.PAGES[0]:
            entered.set()
            assert release.wait(5)
        return fetcher(card("77", p.clock[0] - 1))(url)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(rp.collect, p.engine, p.settings, slow)
        try:
            assert entered.wait(2)
            rp.collect(p.engine, p.settings, lambda *_: pytest.fail("collector overlap"))
        finally:
            release.set()
            work.result(timeout=5)
    assert calls == list(html_shadow.PAGES)
    assert len(probe(p)["pending"]) == 1


@pytest.mark.parametrize("reason", ["rate_limited", "access_denied", "robots_denied"])
def test_denial_or_429_backoff_does_not_create_jobs_or_retry_early(p, monkeypatch, reason):
    setup(p, monkeypatch)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = dict(row.result); data["robots_until"] = 0; row.result = data; db.commit()
    calls = []
    def fail(url):
        calls.append(url)
        if reason == "robots_denied":
            return "User-agent: *\nDisallow: /\n", 28
        raise html_shadow.ProbeError(reason, 1200)
    rp.collect(p.engine, p.settings, fail)
    p.clock[0] += 1199
    rp.collect(p.engine, p.settings, lambda *_: pytest.fail("early retry"))
    assert len(calls) == 1 and not probe(p)["pending"] and not p.sent
    if reason in rp.DENIED:
        p.clock[0] += 3600
        rp.collect(p.engine, p.settings, lambda *_: pytest.fail("denial bypass"))


def test_candidate_queue_is_bounded_and_stale_candidates_spend_no_api_calls(p, monkeypatch):
    setup(p, monkeypatch)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    body = "".join(card(str(300 + n), p.clock[0] - 1) for n in range(150))
    rp.collect(p.engine, p.settings, fetcher(body))
    assert len(probe(p)["pending"]) == 128 and probe(p)["queue_overflow"] == 22
    p.clock[0] += rp.MAX_AGE + 1
    drain(p)
    assert not probe(p)["pending"] and not probe(p)["api"]


def test_additional_api_cap_defers_quote_without_losing_confirmed_job(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    offer(p)
    # Complete the due primary poll, then validate the HTML candidate only.
    p.runner.tick()
    p.runner.tick()
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").state == "pending"
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = dict(row.result); data["api"] = [p.clock[0]] * rp.API_HOURLY; row.result = data
        initial = db.get(SourceBudget, "auto_ria").total
        db.commit()
    p.runner.tick()
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "77").state == "pending"
        assert db.get(MonitorJob, "77").reason == "reserved_for_new_publications"
        assert db.get(SourceBudget, "auto_ria").total == initial
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = dict(row.result); data["api"] = []; row.result = data; db.commit()
    p.clock[0] += 61
    drain(p)
    assert quotes == ["77"] and len(p.sent) == 1


def test_disabling_intake_rebaselines_without_resetting_epochs_or_delivery_claims(p, monkeypatch):
    quotes = setup(p, monkeypatch)
    offer(p)
    with Session(p.engine) as db:
        epoch = db.get(MonitorWatch, 1).epoch
        count = db.get(SourceProbe, rp.PROBE_ID).requests
    p.settings = replace(p.settings, ria_recent_publications_enabled=False)
    p.runner.settings = p.settings
    drain(p)
    assert not probe(p)["pending"] and probe(p)["baseline_at"] is None and not quotes
    with Session(p.engine) as db:
        assert db.get(MonitorWatch, 1).epoch == epoch
        assert db.get(SourceProbe, rp.PROBE_ID).requests == count
    p.settings = replace(p.settings, ria_recent_publications_enabled=True)
    p.runner.settings = p.settings
    p.clock[0] += 61
    rp.collect(p.engine, p.settings, fetcher(card("77", p.clock[0] - 1)))
    drain(p)
    assert not p.sent and not quotes and not probe(p)["pending"]


@pytest.mark.parametrize("cap", ["hourly", "daily", "total"])
def test_html_calls_cannot_consume_primary_reserved_or_last_hard_capacity(p, monkeypatch, cap):
    quotes = setup(p, monkeypatch)
    offer(p)
    # Bring the ordinary feed current before restricting the fallback capacity.
    p.runner.tick()
    with Session(p.engine) as db:
        budget = db.get(SourceBudget, "auto_ria")
        if cap == "hourly": budget.calls = [p.clock[0]] * 720
        if cap == "daily": budget.calls = [p.clock[0] - 4000] * 2400
        if cap == "total": budget.total = 90000
        before = budget.total
        db.commit()
    p.runner.tick()
    assert not details(p, "77") and not quotes and not p.sent
    with Session(p.engine) as db:
        assert db.get(SourceBudget, "auto_ria").total == before
        assert db.get(MonitorJob, "77") is None


def test_due_primary_publication_precedes_html_intake(p, monkeypatch):
    setup(p, monkeypatch)
    offer(p)
    p.ads["124"] = p.clock[0] - 1
    assert p.runner.tick()
    assert not details(p, "77")
    with Session(p.engine) as db:
        assert db.get(MonitorJob, "124").state == "pending"
        assert db.get(MonitorJob, "77") is None
    assert p.runner.tick()
    assert len(details(p, "124")) == 1 and not details(p, "77")
    drain(p)
    dispatch(p)
    assert len(details(p, "77")) == 1


def test_collector_finishes_with_latest_intake_and_budget_state(p, monkeypatch):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    p.clock[0] += rp.interval(p.clock[0]) + 1
    entered, release = threading.Event(), threading.Event()
    body = card("78", p.clock[0] - 1)
    def slow(url):
        if url == html_shadow.PAGES[0]:
            entered.set()
            assert release.wait(5)
        return fetcher(body)(url)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(rp.collect, p.engine, p.settings, slow)
        try:
            assert entered.wait(2)
            drain(p)
            assert len(p.sent) == 1
        finally:
            release.set()
            work.result(timeout=5)
    data = probe(p)
    assert data["validated"] == 1 and len(data["api"]) == 2
    assert set(data["pending"]) == {"78"}


def test_disabled_or_expired_queued_html_delivery_is_cancelled_without_api_refresh(p, monkeypatch):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick()  # Primary poll.
    p.runner.tick()  # One official detail.
    p.runner.tick()  # One AI valuation.
    from backend.worker import enqueue, deliver_one
    enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
    before = len(p.calls)
    settings = replace(p.settings, ria_recent_publications_enabled=False)
    assert deliver_one(p.engine, settings, p.runner.sender) == "cancelled"
    with Session(p.engine) as db:
        assert db.scalar(select(Delivery)).state == "cancelled"
    assert len(p.calls) == before and not p.sent


def test_long_delivery_wait_refreshes_current_price_with_durable_html_proof(p, monkeypatch):
    setup(p, monkeypatch)
    offer(p)
    p.runner.tick()
    p.runner.tick()
    p.runner.tick()
    from backend.worker import enqueue, deliver_one
    enqueue(p.engine, require_provider_range=True, require_confirmed_deal=True)
    p.clock[0] += 301  # The saved price/range exceeds the existing 300s freshness limit.
    p.prices["77"] = 40000
    assert deliver_one(p.engine, p.settings, p.runner.sender) == "pending"
    with Session(p.engine) as db:
        job = db.get(MonitorJob, "77")
        assert job.state == "pending" and rp.valid_proof(job.result)
    drain(p)
    dispatch(p)
    assert len(details(p, "77")) == 2 and not p.sent


def test_http_caps_reserve_before_fetch_and_never_clear_durable_usage(p, monkeypatch):
    setup(p, monkeypatch)
    p.clock[0] += rp.interval(p.clock[0]) + 1
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        data = dict(row.result); data["http"] = [[p.clock[0], rp.HTTP_HOURLY, 1000, "prior"]]
        row.result = data; count = row.requests; db.commit()
    rp.collect(p.engine, p.settings, lambda *_: pytest.fail("HTTP cap bypass"))
    with Session(p.engine) as db:
        row = db.get(SourceProbe, rp.PROBE_ID)
        assert row.status == "http_budget_wait" and row.requests == count
        assert row.result["http"][0][1] == rp.HTTP_HOURLY
