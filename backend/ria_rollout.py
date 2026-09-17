"""Explicit, once-only catalog/pagination rollout check, capped at 32 API calls.

Uses production filtering, parsing and cursors; skips peer valuation in this check.
Does not cache unvalued UI snapshots, ingest listings, or enable notifications.
"""
import time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auto_ria import RiaError, fetch_json
from .models import Filters, SourceProbe
from .ria_search import RiaSearch

PROBE_ID = "catalog-pagination-v1"
CALL_CAP = 32


def check_once(engine, key, enabled, fetch=fetch_json):
    if not enabled or not key:
        return
    with Session(engine) as db:
        db.add(SourceProbe(id=PROBE_ID, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    deadline = time.monotonic() + 85
    calls = 0
    def bounded_fetch(*args):
        nonlocal calls
        if calls >= CALL_CAP or time.monotonic() > deadline:
            raise RiaError("search_limit")
        with Session(engine) as db:
            db.get(SourceProbe, PROBE_ID).requests += 1
            db.commit()
        calls += 1
        return fetch(*args)

    class ListingCheck(RiaSearch):
        def comparisons(self, candidate):
            return []  # Validate result coverage separately from market valuation.

    result, state = {}, "check_failed"
    try:
        catalog = RiaSearch(engine, key, bounded_fetch).catalog()
        result["catalog_counts"] = {name: len(items) for name, items in catalog.items() if isinstance(items, list)}
        models = RiaSearch(engine, key, bounded_fetch).catalog("Peugeot")
        result["model_check"] = {"brand": "Peugeot", "models": len(models["models"]),
                                 "has_3008": any(item["name"] == "3008" for item in models["models"])}
        filters = Filters(brand="Volkswagen", region="Хмельницька область", onlyDeals=False)
        context, pages = None, []
        for _ in range(2):
            client = ListingCheck(engine, key, bounded_fetch)
            data = client.search_uncached(filters, context)
            pages.append({"inspected": data["inspected"], "returned": len(data["cars"]),
                          "ids": [car["id"] for car in data["cars"]], "warnings": data["warnings"],
                          "source_total": data["source_total"]})
            result["pages"] = pages
            if not data["next_cursor"]:
                break
            context = client.resume(filters, data["next_cursor"])
        result["filters"] = filters.canonical()
        result["unique_cars"] = len({i for page in pages for i in page["ids"]})
        state = "verified" if result["unique_cars"] > 3 and len(pages) == 2 and all(page["returned"] for page in pages) else "partial"
    except RiaError as exc:
        state = str(exc)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        row.status, row.result, row.checked_at = state, result, time.time()
        db.commit()


def status(engine):
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        return {"status": row.status, "requests_used": row.requests, **row.result} if row else {"status": "not_checked"}
