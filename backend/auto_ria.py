"""One-time, two-request AUTO.RIA connectivity check; never feeds delivery.

Documentation:
https://docs-developers.ria.com/en/used-cars/auto_search_and_info/search_auto
https://docs-developers.ria.com/en/used-cars/auto_search_and_info/auto_info
"""
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import SourceProbe

PROBE_ID = "auto-ria-connection-v1"


class RiaError(Exception):
    """Only fixed, non-secret error codes may leave this adapter."""

    def __init__(self, code, *, http_status=None, request_attempted=None):
        super().__init__(code)
        self.http_status = http_status
        self.request_attempted = request_attempted


def _attempt_event(callback, event, **fields):
    """Transport evidence only; an unavailable audit never repeats a request."""
    if callback is None:
        return
    try:
        callback({"event": event, **fields})
    except Exception:
        # No exception text, request URL or credentials may enter a fallback log.
        pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(key, method, params, telemetry=None, *, attempt_telemetry=None):
    if attempt_telemetry is None:
        # Diagnostic wrappers retain transport evidence from the active request;
        # direct startup calls outside that context acquire no invented row.
        from .api_attempt_audit import current_observer
        attempt_telemetry = current_observer()
    modification_catalog = bool(re.fullmatch(
        r"modifications/by/generation/[1-9][0-9]{0,11}/body/[1-9][0-9]{0,11}/modifications", method))
    if not modification_catalog and method not in {"search", "info", "states", "type", "categories/1/marks",
                      "categories/1/bodystyles", "categories/1/gearboxes"} and not re.fullmatch(r"categories/1/marks/[1-9][0-9]*/models", method):
        raise RiaError("invalid_method", request_attempted=False)
    base = "https://developers.ria.com/" if modification_catalog else "https://developers.ria.com/auto/"
    url = base + method + "?" + urlencode({**params, "api_key": key})
    attempted, http_status = False, None
    try:
        # urllib emits no request URL logs. Do not print exceptions: URLs contain the key.
        opener = build_opener(NoRedirect())
        request = Request(url, headers={"Accept": "application/json"})
        attempted = True
        _attempt_event(attempt_telemetry, "transport_started")
        with opener.open(request, timeout=8) as response:
            status = getattr(response, "status", None)
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                http_status = int(status)
                _attempt_event(attempt_telemetry, "http_response", http_status=http_status)
            if telemetry:
                # Only these numeric public quota headers; never cookies or URLs.
                quota = {}
                for header, name in (("X-RateLimit-Limit", "hourly_limit"),
                                     ("X-RateLimit-Remaining", "hourly_remaining")):
                    value = response.headers.get(header, "")
                    if value.isascii() and value.isdecimal() and len(value) <= 12:
                        quota[name] = int(value)
                telemetry(quota)
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RiaError("invalid_response", http_status=http_status, request_attempted=attempted)
            return json.loads(raw)
    except HTTPError as exc:
        status = exc.code
        _attempt_event(attempt_telemetry, "http_response", http_status=status)
        # NOT_FOUND may describe an API route, rather than a removed automobile.
        # Preserve uncertainty and pause automatic paid retries for operator review.
        code = ("info_endpoint_unavailable" if method == "info" and status in (404, 410) else
                {401: "key_rejected", 403: "access_denied", 429: "quota_exceeded"}.get(status, "upstream_error"))
        exc.close()
        raise RiaError(code, http_status=status, request_attempted=attempted) from None
    except (URLError, TimeoutError, OSError):
        _attempt_event(attempt_telemetry, "transport_failed")
        raise RiaError("connection_error", http_status=http_status, request_attempted=attempted) from None
    except (ValueError, UnicodeError):
        raise RiaError("invalid_response", http_status=http_status, request_attempted=attempted) from None


def first_id(data):
    try:
        ids = data["result"]["search_result"]["ids"]
        if not isinstance(ids, list):
            raise ValueError()
        if not ids:
            return None
        value = str(ids[0])
        if not re.fullmatch(r"[1-9][0-9]{0,11}", value):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise RiaError("invalid_response") from None


def listing_preview(data, requested_id):
    try:
        auto = data["autoData"]
        if str(auto["autoId"]) != requested_id:
            raise ValueError()
        if auto.get("isSold") is not False or auto.get("active") is not True or auto.get("statusId") != 0:
            raise RiaError("listing_unavailable")
        path = data["linkToView"]
        if not isinstance(path, str) or not re.fullmatch(r"/auto_[a-zA-Z0-9_-]+_" + requested_id + r"\.html", path):
            raise ValueError()
        price = data["USD"]
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            raise ValueError()
        year = auto["year"]
        if type(year) is not int or not 1900 <= year <= 2100:
            raise ValueError()
        title = data["title"]
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise ValueError()
        # Deliberate allowlist: no seller data, VIN, description, tokens or raw payload.
        return {"id": requested_id, "title": title, "year": year,
                "price_usd": price, "url": "https://auto.ria.com" + path}
    except (KeyError, TypeError, ValueError):
        raise RiaError("invalid_response") from None


def probe_once(engine, key, fetch=fetch_json):
    if not key:
        return
    # Claim committed BEFORE any network request. The PK prevents duplicate calls
    # across processes/redeploys. A crash or failure never triggers an automatic retry.
    with Session(engine) as db:
        db.add(SourceProbe(id=PROBE_ID, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    result, status = {}, "error"
    try:
        def request(method, params):
            with Session(engine) as db:
                row = db.get(SourceProbe, PROBE_ID)
                row.requests += 1
                db.commit()
            return fetch(key, method, params)

        source_id = first_id(request("search", {"category_id": 1, "searchType": 4,
                                               "status_id": 0, "countpage": 1,
                                               "page": 0, "order_by": 7}))
        if source_id is None:
            status = "connected_empty"
        else:
            result = listing_preview(request("info", {"auto_id": source_id}), source_id)
            status = "connected"
    except RiaError as exc:
        status = str(exc)
    except Exception:
        # Never log an upstream exception that could contain a credential.
        status = "check_failed"
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        row.status, row.result, row.checked_at = status, result, time.time()
        db.commit()


def probe_status(engine, configured):
    with Session(engine) as db:
        row = db.get(SourceProbe, PROBE_ID)
        search_check = db.get(SourceProbe, "auto-ria-filter-check-v2")
        return {"source": "AUTO.RIA", "source_url": "https://auto.ria.com/",
                "filter_check": {"status": search_check.status, **search_check.result} if search_check else None,
                "status": row.status if row else ("pending" if configured else "not_configured"),
                "checked_at": row.checked_at if row else None,
                "requests_used": row.requests if row else 0,
                "sample": row.result if row else {},
                "mode": "connection_check_only", "market_valuation_ready": False}
