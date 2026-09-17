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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(key, method, params, telemetry=None):
    if method not in {"search", "info", "states", "type", "categories/1/marks",
                      "categories/1/bodystyles", "categories/1/gearboxes"} and not re.fullmatch(r"categories/1/marks/[1-9][0-9]*/models", method):
        raise RiaError("invalid_method")
    url = "https://developers.ria.com/auto/" + method + "?" + urlencode({**params, "api_key": key})
    try:
        # urllib emits no request URL logs. Do not print exceptions: URLs contain the key.
        with build_opener(NoRedirect()).open(Request(url, headers={"Accept": "application/json"}), timeout=8) as response:
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
                raise RiaError("invalid_response")
            return json.loads(raw)
    except HTTPError as exc:
        code = {401: "key_rejected", 403: "access_denied", 429: "quota_exceeded"}.get(exc.code, "upstream_error")
        raise RiaError(code) from None
    except (URLError, TimeoutError, OSError):
        raise RiaError("connection_error") from None
    except (ValueError, UnicodeError):
        raise RiaError("invalid_response") from None


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
