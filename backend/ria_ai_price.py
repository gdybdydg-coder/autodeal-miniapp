"""Paid, listing-specific AUTO.RIA AI valuation; no comparable-car requests.

https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai
The live response adds avgValueRange to the documented avgPrice block. Retain
both source values; never manufacture a range when only an average is supplied.
"""
import json
import logging
import re
import time
from decimal import Decimal, ROUND_FLOOR
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .auto_ria import NoRedirect, RiaError
from .valuation import number

METHOD = "ai-avarage-price"
PERIOD_HOURS = 168
TIMEOUT = 20
MAX_BYTES = 1024 * 1024
PROVIDER_FIELDS = {"average_usd", "range_fraction", "quantity", "period_hours"}
log = logging.getLogger("autodeal.ai_price")
log.setLevel(logging.INFO)


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,11}", value) is not None


def boundaries(provider):
    """Symmetric provider range, rounded down to whole USD (never upwards)."""
    if (not isinstance(provider, dict) or set(provider) != PROVIDER_FIELDS
            or not number(provider["average_usd"], positive=True)
            or not number(provider["range_fraction"], positive=True)
            or provider["range_fraction"] >= 1
            or type(provider["quantity"]) is not int or provider["quantity"] <= 0
            or type(provider["period_hours"]) is not int or provider["period_hours"] != PERIOD_HOURS):
        return None
    mean, radius = Decimal(str(provider["average_usd"])), Decimal(str(provider["range_fraction"]))
    lower = int((mean * (1 - radius)).to_integral_value(rounding=ROUND_FLOOR))
    upper = int((mean * (1 + radius)).to_integral_value(rounding=ROUND_FLOOR))
    return (lower, upper) if lower > 0 else None


def parse_quote(data, source_id, *, now=None):
    from .ria_market_range import BASIS
    if not valid_id(source_id) or not isinstance(data, dict):
        return None
    blocks = data.get("statisticData")
    if not isinstance(blocks, list):
        return None
    blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "avgPrice"]
    if len(blocks) != 1 or not isinstance(blocks[0].get("price"), dict):
        return None
    block = blocks[0]
    provider = {"average_usd": block["price"].get("USD"),
                "range_fraction": block.get("avgValueRange"),
                "quantity": block.get("quantityAdv"), "period_hours": PERIOD_HOURS}
    limits = boundaries(provider)
    if limits is None:
        return None
    # The ID is bound to the single omniId request, not inferred from similarCars.
    # Do not retain similarCars, noticeData, seller IDs, VINs or raw response data.
    return {"source_id": source_id, "basis": BASIS, "currency": "USD",
            "lower_usd": limits[0], "upper_usd": limits[1], "provider": provider,
            "observed_at": time.time() if now is None else now}


def fetch_quote(key, user_id, source_id):
    if not key or not valid_id(user_id) or not valid_id(source_id):
        raise RiaError("ai_not_configured")
    url = "https://developers.ria.com/auto/" + METHOD + "/?" + urlencode(
        {"user_id": user_id, "api_key": key})
    body = json.dumps({"langId": 4, "period": PERIOD_HOURS,
                       "params": {"omniId": source_id}}).encode()
    request = Request(url, data=body, method="POST",
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
    try:
        # No redirects, credentials in logs, retries or browser-access workarounds.
        with build_opener(NoRedirect()).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RiaError("ai_invalid_response")
            return parse_quote(json.loads(raw), source_id)
    except HTTPError as exc:
        # Method permission failures must not stop the ordinary new-listing feed.
        code = {401: "ai_access_denied", 403: "ai_access_denied",
                429: "quota_exceeded"}.get(exc.code, "ai_upstream_error")
        raise RiaError(code) from None
    except (URLError, TimeoutError, OSError):
        raise RiaError("ai_connection_error") from None
    except (ValueError, UnicodeError):
        raise RiaError("ai_invalid_response") from None


def check_once(engine, key, user_id, source_id):
    """Operator-only read-only quote; never creates a job, match or delivery."""
    if not source_id:
        return
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    from .models import SourceProbe
    from .ria_search import RiaSearch
    probe_id = "auto-ria-ai-range-v1-" + source_id
    with Session(engine) as db:
        db.add(SourceProbe(id=probe_id, status="checking", checked_at=time.time(), requests=0, result={}))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    source = RiaSearch(engine, key)
    source.request_limit = 1
    status, result, acquired = "error", {}, False
    try:
        source.acquire()
        acquired = True
        quote = source.market_range(source_id, user_id)
        if quote:
            status, result = "verified", quote
        else:
            status = "range_unavailable"
    except RiaError as exc:
        status = str(exc)
    finally:
        if acquired:
            source.release()
    with Session(engine) as db:
        row = db.get(SourceProbe, probe_id)
        row.status, row.result, row.requests = status, result, source.requests_made
        db.commit()
    log.info("AUTO.RIA AI range probe source_id=%s status=%s requests=%s lower_usd=%s upper_usd=%s provider=%s",
             source_id, status, source.requests_made, result.get("lower_usd"), result.get("upper_usd"),
             result.get("provider"))
