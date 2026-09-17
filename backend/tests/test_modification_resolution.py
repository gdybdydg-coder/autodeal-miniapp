import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from backend import auto_ria
from backend.models import Filters
from backend.ria_search import RiaSearch, estimate, parse_car, parse_modification_catalog
from backend.ria_validation import comparison_report, validate_once, validation_status
from backend.tests.test_ria_search import engine, raw, configure_caps
from backend.tests.test_valuation_audit import popular_fixture

LABEL = "2.0 TDI DSG (150 к.с.)"
PATH = "modifications/by/generation/10/body/4/modifications"


def missing_car(source_id="123", **overrides):
    data = raw(source_id, **overrides)
    data["autoData"].update(modificationId=None, modificationName=LABEL)
    return data


def test_unique_catalog_recovers_candidate_and_peers_without_relaxing_comparison(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls = []

    def fetch(key, path, params):
        calls.append((path, params))
        if path == PATH:
            return [{"name": LABEL, "value": 20}, {"name": LABEL + " 4Motion", "value": 21}]
        if path == "search":
            ids = [str(i) for i in range(124, 129)] if "generation_id[0][0]" in params else ["123", "129"]
            return {"result": {"search_result": {"ids": ids, "count": len(ids)}}}
        return missing_car(params["auto_id"], USD=8500 if params["auto_id"] == "123" else 10000)

    result = RiaSearch(engine, "private-key", fetch).search(Filters(onlyDeals=False))
    car = result["cars"][0]
    assert car["market"] == 10000 and car["comparables"] == 5
    assert car["discount"] == 15 and car["modification_source"] == "catalog_name"
    assert car["modification_id"] == 20 and car["valuation_reasons"] == []
    assert [path for path, _ in calls[:3]] == ["search", "info", "info"]
    assert sum(path == PATH for path, _ in calls) == 1
    count = len(calls)
    RiaSearch(engine, "private-key", fetch).search(Filters(onlyDeals=False))
    assert len(calls) == count


@pytest.mark.parametrize("names,status", [
    ([(LABEL, 20), (LABEL, 21)], "ambiguous"),
    ([(LABEL + " 4Motion", 21)], "not_found"),
    ([(LABEL.replace("150", "190"), 22)], "not_found"),
    ([(LABEL.replace("DSG", "MT"), 23)], "not_found"),
])
def test_ambiguous_or_different_full_labels_never_fill_an_id(engine, names, status):
    candidate = parse_car(missing_car(), "123")
    calls = []

    def fetch(key, path, params):
        calls.append(path)
        return [{"name": name, "value": value} for name, value in names]

    search = RiaSearch(engine, "key", fetch)
    search.acquire()
    try:
        assert search.comparisons(candidate) == []
    finally:
        search.release()
    assert calls == [PATH] and candidate["modification_id"] is None
    assert candidate["modification_resolution"] == status
    assert estimate(candidate, [parse_car(raw(str(i)), str(i)) for i in range(124, 129)])["market"] is None


def test_only_case_and_whitespace_normalization_and_no_truncated_catalog_match(engine):
    candidate = parse_car(missing_car(), "123")
    search = RiaSearch(engine, "key", lambda *_: [{"name": "  2.0   tdi dsg (150 к.с.)  ", "value": 20}])
    search.acquire()
    try:
        search.resolve_modification(candidate)
    finally:
        search.release()
    assert candidate["modification_id"] == 20
    with pytest.raises(auto_ria.RiaError, match="invalid_response"):
        parse_modification_catalog([{"name": "x" * 151, "value": 1}])
    data = missing_car()
    data["autoData"]["modificationName"] = "x" * 151
    assert parse_car(data, "123")["modification_name"] == ""


def test_existing_id_unknown_condition_or_missing_context_never_spend_catalog_calls(engine):
    search = RiaSearch(engine, "key", lambda *_: pytest.fail("unnecessary provider request"))
    base = parse_car(missing_car(), "123")
    for changes in ({"modification_id": 999}, {"comparable_condition": False},
                    {"generation_id": None}, {"body_id": None}, {"gear_id": None},
                    {"modification_name": ""}):
        candidate = {**base, **changes}
        search.resolve_modification(candidate)
        if changes.get("modification_id"):
            assert candidate["modification_id"] == 999
        else:
            assert candidate["modification_id"] is None


def test_catalog_quota_retains_all_loaded_candidates_for_continuation(engine, monkeypatch):
    configure_caps(monkeypatch)

    def fetch(key, path, params):
        if path == PATH:
            raise auto_ria.RiaError("quota_exceeded")
        if path == "search":
            return {"result": {"search_result": {"ids": ["123", "124"], "count": 2}}}
        return missing_car(params["auto_id"])

    result = RiaSearch(engine, "key", fetch).search(Filters(onlyDeals=False))
    assert [car["id"] for car in result["cars"]] == ["123", "124"]
    assert result["pending_valuations"] == 2 and result["next_cursor"]
    assert result["warnings"] == ["quota_exceeded"]
    assert all(car["market"] is None and car["valuation"] == "pending" for car in result["cars"])


def test_retained_live_evidence_keeps_medians_and_all_rejected_conditions():
    data = json.loads((Path(__file__).parents[1] / "docs/valuation-audit-2026-09-17.json").read_text())["valuation_check"]
    valued = 0
    for query in data["queries"]:
        cars = {car["id"]: car for car in query["cars"]}
        for report in query["comparisons"]:
            candidate = copy.deepcopy(cars[report["candidate_id"]])
            as_of = max(car["observed_at"] for car in [candidate, *report["peers"]])
            replay = estimate(candidate, report["peers"], now=as_of)
            assert replay["market"] == report["recalculated_market"]
            assert replay["comparables"] == len(report["accepted_ids"])
            assert comparison_report(candidate, report["peers"], now=as_of)["calculation_matches"]
            valued += replay["market"] is not None
    assert valued == 1


def test_supported_condition_audit_is_labeled_and_still_checks_returned_details(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls = []
    fixture = popular_fixture(calls)

    def fetch(key, path, params):
        if path == "search":
            assert params["damage"] == 1
        data = fixture(key, path, params)
        if path == "info" and int(params["auto_id"]) // 1000 == 6:
            data["technicalCondition"] = {"id": 3}
        return data

    validate_once(engine, "key", "eligible-test", fetch, profile="eligible-v1")
    result = validation_status(engine, "eligible-test", "eligible-v1")
    assert result["candidate_scope"] == "undamaged" and result["status"] == "partial"
    assert result["queries"][1]["valued"] == 0
    assert result["requests_used"] <= 96


def test_audit_checks_real_catalog_lookup_against_the_listings_own_id(engine, monkeypatch):
    configure_caps(monkeypatch)
    calls = []
    fixture = popular_fixture(calls)

    def fetch(key, path, params):
        if path == PATH:
            calls.append((path, params))
            return [{"name": LABEL, "value": 20}]
        data = fixture(key, path, params)
        if path == "info":
            data["autoData"]["modificationName"] = LABEL
        return data

    validate_once(engine, "key", "catalog-check", fetch, profile="eligible-v1")
    result = validation_status(engine, "catalog-check", "eligible-v1")
    assert result["status"] == "samples_checked"
    assert sum(path == PATH for path, _ in calls) == 1
    for query in result["queries"]:
        check = query["catalog_resolution_checks"][0]
        assert check["status"] == "matched"
        assert check["listing_modification_id"] == check["catalog_modification_id"] == 20
        assert query["cars"][0]["modification_source"] == "listing"


def test_catalog_transport_uses_only_the_documented_host_and_numeric_path(monkeypatch):
    paths = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _): return b'[]'

    class Opener:
        def open(self, request, **kwargs):
            url = urlsplit(request.full_url)
            assert url.scheme == "https" and url.hostname == "developers.ria.com"
            paths.append(url.path)
            return Response()

    monkeypatch.setattr(auto_ria, "build_opener", lambda *_: Opener())
    assert auto_ria.fetch_json("fake-key", PATH, {}) == []
    assert paths == ["/" + PATH]
    for bad in ("https://evil.test", PATH + "?url=elsewhere", PATH.replace("10", "../10"),
                PATH.replace("10", "0"), "modifications/by/generation/10/modifications"):
        with pytest.raises(auto_ria.RiaError, match="invalid_method"):
            auto_ria.fetch_json("fake-key", bad, {})
    assert len(paths) == 1
