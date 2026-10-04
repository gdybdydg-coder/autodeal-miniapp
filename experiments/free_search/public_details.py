"""Offline experiment: public JSON-LD only; no network, DB or delivery imports.

Input is supplied by a caller. A parsed Vehicle is never publication/category
proof and never authorizes a notification. Values are assertions of the page,
not independently verified live prices. No seller/contact/VIN/raw HTML retained.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

MAX_HTML = 2_500_000
MAX_SCRIPT = 256_000
MAX_SCRIPTS = 64
MAX_NODES = 500


class ParseError(ValueError):
    """A stable code, never the raw input or personal data."""


@dataclass(frozen=True)
class PublicDetails:
    listing_id: str
    brand: str | None
    model: str | None
    year: int | None
    body: str | None
    fuel: str | None
    transmission: str | None
    mileage_km: Decimal | None
    price: Decimal | None
    currency: str | None
    availability: str
    condition: str | None
    missing: tuple[str, ...]
    issues: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]
    ready_for_delivery: bool = False


def _url_id(value):
    if not isinstance(value, str):
        raise ParseError("invalid_identity")
    try:
        p = urlsplit(value)
    except ValueError:
        raise ParseError("invalid_identity") from None
    if p.scheme != "https" or p.netloc != "auto.ria.com" or p.query:
        raise ParseError("invalid_identity")
    m = re.fullmatch(r"/(?:uk/)?auto_[a-z0-9_]+_([1-9][0-9]{0,11})\.html", p.path)
    if not m:
        raise ParseError("invalid_identity")
    return m[1]


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks, self.parts, self.canonicals = [], None, []
        self.length = 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "link" and "canonical" in (data.get("rel") or "").lower().split():
            self.canonicals.append(data.get("href"))
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            if len(self.blocks) >= MAX_SCRIPTS:
                raise ParseError("too_many_scripts")
            self.parts, self.length = [], 0

    def handle_data(self, data):
        if self.parts is not None:
            self.length += len(data)
            if self.length > MAX_SCRIPT:
                raise ParseError("script_oversize")
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.parts is not None:
            self.blocks.append("".join(self.parts))
            self.parts = None


def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise ParseError("duplicate_json_key")
        out[k] = v
    return out


def _text(value):
    if isinstance(value, dict):
        value = value.get("name")
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    return value.strip()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if len(str(value)) > 40:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and abs(result) <= Decimal("1e12") else None


def parse_public_details(html: str, expected_id: str) -> PublicDetails:
    if not isinstance(expected_id, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", expected_id):
        raise ParseError("invalid_expected_id")
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML:
        raise ParseError("html_oversize")
    parser = _Scripts()
    parser.feed(html)
    if any(_url_id(url) != expected_id for url in parser.canonicals):
        raise ParseError("canonical_mismatch")
    if parser.parts is not None:
        raise ParseError("unterminated_jsonld")
    vehicles, queue = [], []
    for block in parser.blocks:
        try:
            queue.append(json.loads(block, object_pairs_hook=_pairs, parse_float=Decimal,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ParseError("invalid_json_number"))))
        except ParseError:
            raise
        except (ValueError, RecursionError):
            raise ParseError("invalid_jsonld") from None
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        if visited > MAX_NODES:
            raise ParseError("too_many_nodes")
        if isinstance(node, list):
            queue.extend(node)
        elif isinstance(node, dict):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and "Vehicle" in types:
                ids = []
                for key in ("@id", "url", "mainEntityOfPage"):
                    value = node.get(key)
                    if value is not None:
                        if isinstance(value, dict):
                            value = value.get("@id")
                        ids.append(_url_id(value))
                if not ids or any(i != expected_id for i in ids):
                    raise ParseError("identity_mismatch")
                vehicles.append(node)
            if "@graph" in node:
                queue.append(node["@graph"])
    if not vehicles:
        raise ParseError("no_vehicle")
    values, issues = {}, set()

    def add(key, value):
        if value is None:
            return
        if key in values and values[key] != value:
            raise ParseError("conflicting_" + key)
        values[key] = value

    for v in vehicles:
        for field, source in (("brand", "brand"), ("model", "model"), ("body", "bodyType"),
                              ("fuel", "fuelType"), ("transmission", "vehicleTransmission"),
                              ("condition", "itemCondition")):
            val = _text(v.get(source))
            if source in v and val is None:
                issues.add("invalid_" + field)
            if field == "body" and val and val.casefold() in {"легкові", "вантажівки"}:
                # Real public JSON-LD currently puts transport categories here.
                # Retain unknown body style; do not create a false filter match.
                issues.add("body_type_is_transport_category")
                val = None
            add(field, val)
        engine = v.get("vehicleEngine")
        if isinstance(engine, dict) and "fuelType" in engine:
            val = _text(engine["fuelType"])
            if val is None:
                issues.add("invalid_fuel")
            add("fuel", val)
        if "productionDate" in v:
            y = str(v["productionDate"])
            if re.fullmatch(r"(?:19|20)[0-9]{2}", y):
                add("year", int(y))
            else:
                issues.add("invalid_year")
        if "mileageFromOdometer" in v:
            odo = v["mileageFromOdometer"]
            n = _number(odo.get("value")) if isinstance(odo, dict) else None
            if not isinstance(odo, dict) or odo.get("unitCode") not in ("KMT", "KM") or n is None or n < 0:
                issues.add("invalid_mileage")
            else:
                add("mileage_km", n)
        offers = v.get("offers", [])
        offers = [offers] if isinstance(offers, dict) else offers
        if not isinstance(offers, list) or len(offers) > 20:
            raise ParseError("invalid_offers")
        for offer in offers:
            if not isinstance(offer, dict) or offer.get("@type") != "Offer":
                raise ParseError("invalid_offer")
            for key in ("@id", "url"):
                if key in offer and _url_id(offer[key]) != expected_id:
                    raise ParseError("offer_identity_mismatch")
            n = _number(offer.get("price"))
            if n is None or n <= 0:
                raise ParseError("invalid_price")
            currency = offer.get("priceCurrency")
            if currency not in ("USD", "EUR", "UAH"):
                raise ParseError("invalid_currency")
            add("price", n)
            add("currency", currency)
            raw = offer.get("availability")
            statuses = {"https://schema.org/InStock": "active",
                        "https://schema.org/OutOfStock": "unavailable",
                        "https://schema.org/SoldOut": "unavailable",
                        "https://schema.org/Discontinued": "unavailable"}
            add("availability", statuses.get(raw, "unknown") if isinstance(raw, str) else "unknown")
            if offer.get("itemCondition") is not None:
                add("condition", _text(offer["itemCondition"]))
    fields = ("brand", "model", "year", "body", "fuel", "transmission", "mileage_km", "price", "currency", "condition")
    if values.get("currency") not in {None, "USD"}:
        issues.add("non_usd_price")
    if values.get("availability", "unknown") != "active":
        issues.add("availability_not_active")
    missing = tuple(k for k in fields if k not in values)
    return PublicDetails(listing_id=expected_id, **{k: values.get(k) for k in fields},
                         availability=values.get("availability", "unknown"), missing=missing,
                         issues=tuple(sorted(issues)),
                         provenance=tuple((k, "public_jsonld") for k in sorted(values)))
