"""Replay identical offline scenarios against two checkouts.

Run this file with runpy and PYTHONPATH pointing at the checkout under review.
It imports that checkout's fixture/code, not this file's parent directory. Never
starts the application lifespan. All provider/Telegram transports are fake.

Example (from a directory outside either checkout):
  PYTHONPATH=/path/to/checkout:/path/to/test/site-packages python -c \
    "import runpy,sys; sys.argv=['replay','--label','baseline']; \
     runpy.run_path('/path/to/ria_cost_replay.py',run_name='__main__')"
"""
import argparse
import json
import logging
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError

from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.orm import Session

# Import both independent metadata registries before creating fixture tables.
from backend import auto_ria, billing
from backend.billing_models import BillingControl, Entitlement
from backend.manual_payment_models import ManualBase
from backend.models import MonitorJob, Range, Search, SourceBudget
from backend.monitor import reset_watch
from backend.tests.test_monitor import add_search, drain, p as fixture, wake
from backend.tests.test_ria_ai_price import enable


@contextmanager
def isolated_fixture():
    with tempfile.TemporaryDirectory(prefix="ria-cost-sameflow-") as directory:
        patch = MonkeyPatch()
        def forbidden_network(*args, **kwargs):
            raise AssertionError("Real network is forbidden in this replay")
        patch.setattr(socket, "create_connection", forbidden_network)
        generator = fixture.__wrapped__(Path(directory), patch)
        p = next(generator)
        try:
            ManualBase.metadata.create_all(p.engine)
            yield p, patch
        finally:
            try:
                next(generator)
            except StopIteration:
                pass
            patch.undo()


def enforce(p, paid):
    with Session(p.engine) as db:
        db.merge(BillingControl(id=billing.CONTROL, sales=True, enforce=True, offer={}))
        for uid in paid:
            db.merge(Entitlement(user_id=uid, expires_at=p.clock[0] + 86400,
                                 updated_at=p.clock[0]))
        # Align activation boundaries: no incidental late-join window split.
        for search in db.scalars(select(Search).order_by(Search.id)):
            reset_watch(db, search.id, True)
        db.commit()
    p.clock[0] += 2


def saved_filters(p):
    with Session(p.engine) as db:
        return [(s.id, s.enabled, s.filters, s.fingerprint)
                for s in db.scalars(select(Search).order_by(Search.id))]


def counters(p, quotes, paid):
    methods = {name: sum(path == name for path, _ in p.calls) for name in ("search", "info")}
    catalog = len(p.calls) - methods["search"] - methods["info"]
    with Session(p.engine) as db:
        budget_total = db.get(SourceBudget, "auto_ria").total
    return {"search": methods["search"], "detail": methods["info"], "valuation": len(quotes),
        "catalog": catalog, "fake_provider_dispatches": len(p.calls) + len(quotes),
        "fake_telegram_acceptances": len(p.sent),
        "paid_acceptances": sum(uid in paid for uid, _ in p.sent),
        "unpaid_acceptances": sum(uid not in paid for uid, _ in p.sent),
        "local_budget_total_including_existing_seed": budget_total}


def difference(after, before):
    return {key: after[key] - before[key] for key in after}


def stage(p, quotes, paid, work):
    before = counters(p, quotes, paid)
    started = time.perf_counter()
    work()
    return {"counts": difference(counters(p, quotes, paid), before),
            "offline_elapsed_seconds": round(time.perf_counter() - started, 6)}


def recipients_case(kind):
    with isolated_fixture() as (p, patch):
        if kind in {"two_paid_one_unpaid_distinct", "identical_paid", "two_distinct_paid_one_unpaid"}:
            add_search(p, sid=2, uid=222,
                **({"price": Range(to=20000)} if kind == "two_distinct_paid_one_unpaid" else {}))
        if kind in {"two_paid_one_unpaid_distinct", "two_distinct_paid_one_unpaid"}:
            add_search(p, sid=3, uid=333, price=Range(to=21000))
        paid = set() if kind == "only_unpaid" else {111, 222}
        enforce(p, paid)
        quotes = enable(p, patch)
        original = saved_filters(p)
        initial = stage(p, quotes, paid, lambda: drain(p))
        p.ads["124"] = p.clock[0] + 1
        def arrival():
            wake(p)
            drain(p)
            for _ in range(4):
                p.runner.deliver_tick()
        publication = stage(p, quotes, paid, arrival)
        return {"initial_poll": initial, "new_publication": publication,
            "total_counts": counters(p, quotes, paid),
            "saved_filters_and_enabled_retained": saved_filters(p) == original,
            "fake_paid_clients": len(paid), "expected_paid_recipients": len(paid),
            "real_network_requests": 0, "real_telegram_requests": 0}


def persistent_info_404(cycles=60):
    with isolated_fixture() as (p, patch):
        paid = {111}
        enforce(p, paid)
        quotes = enable(p, patch)
        original = saved_filters(p)
        drain(p)
        factory = p.runner.search_factory
        transport_calls = []
        class ErrorOpener:
            def open(self, request, timeout):
                transport_calls.append(timeout)
                raise HTTPError("https://test.invalid/", 404, "synthetic", {}, None)
        patch.setattr(auto_ria, "build_opener", lambda *args: ErrorOpener())
        def source_factory(engine, key):
            source = factory(engine, key)
            fetch = source.fetch
            def wrapped(api_key, path, params):
                if path == "info" and params.get("auto_id") == "124":
                    p.calls.append((path, dict(params)))
                    return auto_ria.fetch_json(api_key, path, params)
                return fetch(api_key, path, params)
            source.fetch = wrapped
            return source
        p.runner.search_factory = source_factory
        p.ads["124"] = p.clock[0] + 1
        def work():
            for _ in range(cycles):
                wake(p)
                drain(p)
        measured = stage(p, quotes, paid, work)
        with Session(p.engine) as db:
            job = db.get(MonitorJob, "124")
            retained_job = {"state": job.state, "reason": job.reason, "attempts": job.attempts}
        return {"cycles": cycles, "fake_clock_step_seconds": 60,
            "flow": measured, "fake_info_404_transport_invocations": len(transport_calls),
            "retained_job": retained_job,
            "saved_filters_and_enabled_retained": saved_filters(p) == original,
            "real_network_requests": 0, "real_telegram_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    # Fixture logs contain only synthetic facts, but JSON output needs no logs.
    logging.disable(logging.CRITICAL)
    started = time.perf_counter()
    cases = {kind: recipients_case(kind) for kind in (
        "two_paid_one_unpaid_distinct", "only_unpaid", "identical_paid", "two_distinct_paid_one_unpaid")}
    cases["persistent_info_404"] = persistent_info_404()
    print(json.dumps({"label": args.label, "data_kind": "synthetic_offline_sameflow",
        "cases": cases, "elapsed_seconds": round(time.perf_counter() - started, 6),
        "provider_charged_units": None, "live_behavior_measured": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
