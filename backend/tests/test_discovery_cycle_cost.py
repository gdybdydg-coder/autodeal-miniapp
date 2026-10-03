"""Exact discovery-query sharing: isolated SQLite and fake provider only."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auto_ria import RiaError
from backend.models import Filters, SourceBudget
from backend.ria_budget import BudgetLimits
from backend.ria_search import RiaSearch, matches, parse_car, parse_ids
from backend.tests.test_ria_search import engine, fixture_fetch, raw


def page(params=None):
    return {"category_id": 1, "page": 0, "countpage": 50, "order_by": 7,
            "published_after": "2026-10-03T08:00:00Z",
            "published_before": "2026-10-03T09:00:01Z", **(params or {})}


def source(engine, fetch):
    return RiaSearch(engine, "isolated-test-key", fetch, BudgetLimits(900, 3000, 90000))


def test_optional_profiles_share_exact_page_but_keep_independent_post_filters(engine):
    calls = []
    client = source(engine, fixture_fetch(calls))
    client.acquire()
    try:
        diesel = Filters(brand="Volkswagen", model="Golf", fuel=["Дизель"])
        automatic = Filters(brand="Volkswagen", model="Golf", transmission=["Автомат"],
                            body=["Хетчбек"], mileage={"to": 200})
        first_params, first_ids = client.discovery_parameters(diesel)
        second_params, second_ids = client.discovery_parameters(automatic)
        assert diesel.fingerprint() != automatic.fingerprint()
        assert first_params == second_params
        first_result = client.discovery_page(first_params | page())
        second_result = client.fork().discovery_page(second_params | page())
        assert first_result == second_result == {"ids": ["123"], "total": 1}
        assert len([call for call in calls if call[0] == "search"]) == 1

        candidate = parse_car(raw(), "123")
        candidate["fuel_id"] = 99
        assert not matches(candidate, diesel, first_ids)
        assert matches(candidate, automatic, second_ids)
        candidate.update(body_id=None, fuel_id=None, gear_id=None, mileage=None)
        assert matches(candidate, diesel, first_ids)
        assert matches(candidate, automatic, second_ids)
    finally:
        client.release()


def test_new_cycle_forces_fresh_page_and_copies_cached_payload(engine):
    calls = []
    client = source(engine, fixture_fetch(calls))
    client.acquire()
    child = client.fork()
    result = child.discovery_page(page())
    result["ids"].append("999")
    assert client.discovery_page(page())["ids"] == ["123"]
    client.release()
    with pytest.raises(RiaError, match="busy"):
        child.discovery_page(page())
    client.acquire()
    try:
        assert client.discovery_page(page())["ids"] == ["123"]
    finally:
        client.release()
    assert len(calls) == 2


def test_parallel_forks_share_one_successful_forced_page(engine):
    calls = []
    started, proceed = threading.Event(), threading.Event()
    base = fixture_fetch(calls)

    def fetch(*args):
        started.set()
        assert proceed.wait(5)
        return base(*args)

    client = source(engine, fetch)
    client.acquire()
    try:
        children = [client.fork() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(child.discovery_page, page()) for child in children]
            try:
                assert started.wait(2)
                children[0].release()  # A child cannot end its parent's cycle.
            finally:
                proceed.set()
            results = [future.result(timeout=5) for future in futures]
        assert results == [{"ids": ["123"], "total": 1}] * 4
        results[0]["ids"].append("999")
        assert results[1]["ids"] == ["123"]
        assert len(calls) == 1
        assert sum(child.requests_made for child in children) == 1
    finally:
        client.release()


@pytest.mark.parametrize("error,attempts", [("upstream_error", 2), ("quota_exceeded", 1)])
def test_failure_is_not_cached_and_existing_budget_policy_controls_retries(engine, error, attempts):
    calls = []

    def fetch(*args):
        calls.append(args)
        raise RiaError(error)

    client = source(engine, fetch)
    client.acquire()
    try:
        for _ in range(2):
            with pytest.raises(RiaError, match=error):
                client.discovery_page(page())
        assert len(calls) == attempts
        with Session(engine) as db:
            assert db.scalar(select(SourceBudget.total)) == 2 + attempts
    finally:
        client.release()


@pytest.mark.parametrize("difference", [
    {"page": 1}, {"published_after": "2026-10-03T08:01:00Z"},
    {"published_before": "2026-10-03T09:01:01Z"}, {"marka_id[0]": 85},
    {"price_do": 3000}, {"state[0]": 2},
])
def test_different_discovery_parameters_are_never_merged(engine, difference):
    calls = []
    client = source(engine, fixture_fetch(calls))
    client.acquire()
    try:
        client.discovery_page(page())
        client.discovery_page(page(difference))
        assert len(calls) == 2
    finally:
        client.release()


def test_general_force_requests_remain_fresh_each_time(engine):
    calls = []
    client = source(engine, fixture_fetch(calls))
    client.acquire()
    try:
        for _ in range(2):
            client.request("search", page(), parse_ids, force=True)
        assert len(calls) == 2
    finally:
        client.release()


def test_new_paid_request_rechecks_access_but_same_cycle_cache_is_free(engine):
    calls, checks = [], []
    eligible = [True]
    client = source(engine, fixture_fetch(calls))

    def policy(db, now, limits):
        checks.append(eligible[0])
        if not eligible[0]:
            raise RiaError("no_eligible_subscription")

    client.request_policy = policy
    client.acquire()
    client.discovery_page(page())
    eligible[0] = False
    assert client.discovery_page(page()) == {"ids": ["123"], "total": 1}
    client.release()
    client.acquire()
    try:
        with pytest.raises(RiaError, match="no_eligible_subscription"):
            client.discovery_page(page())
        assert len(calls) == 1 and checks == [True, False]
    finally:
        client.release()
