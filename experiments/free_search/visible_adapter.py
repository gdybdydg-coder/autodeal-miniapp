"""Fixture-only adapter from visible public facts to the filter gate.

No network, paid API, database, backend or delivery imports.  The parser keeps
only allowlisted evidence and never returns seller text, contacts, VIN or HTML.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
import re
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from experiments.free_search.filter_gate import PublicEvidence
from experiments.free_search.public_cards import Candidate
from experiments.free_search.public_details import PublicDetails


KYIV = ZoneInfo("Europe/Kyiv")
MAX_HTML = 2_500_000
MAX_TEXT = 500_000
HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})

CATEGORY_LABELS = {
    "Легкові з пробігом": "passenger",
    "Мотоцикли з пробігом": "motorcycle",
    "Мото з пробігом": "motorcycle",
    "Вантажівки з пробігом": "truck",
    "Автобуси з пробігом": "bus",
    "Спецтехніка з пробігом": "special",
}

REGIONS = frozenset({
    "Вінницька область", "Волинська область", "Дніпропетровська область",
    "Донецька область", "Житомирська область", "Закарпатська область",
    "Запорізька область", "Івано-Франківська область", "Київська область",
    "Кіровоградська область", "Луганська область", "Львівська область",
    "Миколаївська область", "Одеська область", "Полтавська область",
    "Рівненська область", "Сумська область", "Тернопільська область",
    "Харківська область", "Херсонська область", "Хмельницька область",
    "Черкаська область", "Чернівецька область", "Чернігівська область",
})


class VisibleParseError(ValueError):
    """Stable reason code; never contains source HTML or personal data."""


@dataclass(frozen=True)
class VisibleFacts:
    listing_id: Optional[str]
    category: Optional[str]
    region: Optional[str]
    created_date: Optional[date]
    issues: Tuple[str, ...]
    provenance: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class AdaptedEvidence:
    evidence: PublicEvidence
    issues: Tuple[str, ...]
    ready_for_delivery: bool = False


def _clean(value):
    return " ".join(value.split())


class _Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.before_h1 = True
        self.anchor_parts = None
        self.anchor_before_h1 = False
        self.anchors = []
        self.text_parts = []
        self.text_length = 0
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "h1":
            self.before_h1 = False
        if tag == "a" and self.anchor_parts is None:
            self.anchor_parts = []
            self.anchor_before_h1 = self.before_h1

    def handle_data(self, data):
        if self.hidden_depth:
            return
        self.text_length += len(data)
        if self.text_length > MAX_TEXT:
            raise VisibleParseError("text_oversize")
        self.text_parts.append(data)
        if self.anchor_parts is not None:
            self.anchor_parts.append(data)

    def handle_endtag(self, tag):
        if tag in HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag == "a" and self.anchor_parts is not None:
            text = _clean("".join(self.anchor_parts))
            if text:
                self.anchors.append((text, self.anchor_before_h1))
            self.anchor_parts = None


def parse_visible_facts(html, expected_id):
    """Parse exact visible labels from a caller-supplied detail-page fixture.

    Category and region evidence must occur in pre-title breadcrumb links.
    Footer/recommendation links after the first H1 are ignored.  The exact
    visible creation label and listing ID must be unique and consistent.
    """
    if not isinstance(expected_id, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", expected_id):
        raise VisibleParseError("invalid_expected_id")
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML:
        raise VisibleParseError("html_oversize")
    parser = _Visible()
    parser.feed(html)
    if parser.anchor_parts is not None or parser.hidden_depth:
        raise VisibleParseError("unterminated_anchor")

    issues = set()
    breadcrumb = [text for text, before in parser.anchors if before]
    categories = {CATEGORY_LABELS[text] for text in breadcrumb if text in CATEGORY_LABELS}
    regions = {text for text in breadcrumb if text in REGIONS}
    category = next(iter(categories)) if len(categories) == 1 else None
    region = next(iter(regions)) if len(regions) == 1 else None
    if not categories:
        issues.add("missing_category")
    elif len(categories) != 1:
        issues.add("conflicting_category")
    if not regions:
        issues.add("missing_region")
    elif len(regions) != 1:
        issues.add("conflicting_region")

    text = _clean(" ".join(parser.text_parts))
    identifiers = set(re.findall(r"\bID\s+авто\s+([1-9][0-9]{0,11})\b", text))
    listing_id = next(iter(identifiers)) if len(identifiers) == 1 else None
    if not identifiers:
        issues.add("missing_listing_id")
    elif len(identifiers) != 1:
        issues.add("conflicting_listing_id")
    elif listing_id != expected_id:
        issues.add("listing_id_mismatch")

    raw_dates = set(re.findall(
        r"Оголошення\s+створене\s+([0-3]?[0-9]\.[01]?[0-9]\.(?:19|20)[0-9]{2})", text))
    dates = set()
    for raw in raw_dates:
        try:
            dates.add(datetime.strptime(raw, "%d.%m.%Y").date())
        except ValueError:
            issues.add("invalid_created_date")
    created = next(iter(dates)) if len(dates) == 1 and len(raw_dates) == 1 else None
    if not raw_dates:
        issues.add("missing_created_date")
    elif len(raw_dates) != 1 or len(dates) != 1:
        issues.add("conflicting_or_invalid_created_date")

    provenance = []
    if category is not None:
        provenance.append(("category", "pre_title_breadcrumb"))
    if region is not None:
        provenance.append(("region", "pre_title_breadcrumb"))
    if created is not None:
        provenance.append(("created_date", "visible_creation_label"))
    if listing_id is not None:
        provenance.append(("listing_id", "visible_listing_id"))
    return VisibleFacts(listing_id, category, region, created,
                        tuple(sorted(issues)), tuple(provenance))


def _mileage(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    integral = value.to_integral_value()
    return int(integral) if value == integral and integral <= 2_000_000 else None


def adapt_candidate(candidate, details, visible):
    """Join three independent evidence stages without authorizing delivery."""
    if not isinstance(candidate, Candidate) or not isinstance(details, PublicDetails) or not isinstance(visible, VisibleFacts):
        raise VisibleParseError("invalid_adapter_input")
    issues = set(visible.issues)
    ids = {candidate.listing_id, details.listing_id, visible.listing_id}
    identity_ok = None not in ids and len(ids) == 1
    if not identity_ok:
        issues.add("identity_conflict")

    try:
        added_date = datetime.fromtimestamp(candidate.added_at, KYIV).date()
    except (TypeError, ValueError, OverflowError, OSError):
        added_date = None
        issues.add("invalid_candidate_add_date")
    date_ok = visible.created_date is not None and added_date == visible.created_date
    if not date_ok:
        issues.add("creation_date_mismatch")

    price = details.price if details.currency == "USD" else None
    if details.currency != "USD":
        issues.add("details_not_usd")
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        price = None
        issues.add("positive_current_price_not_proven")
    if candidate.preview_usd is not None and price is not None and candidate.preview_usd != price:
        issues.add("preview_price_changed")

    active = True if details.availability == "active" else False if details.availability == "unavailable" else None
    if active is not True:
        issues.add("active_status_not_proven")
    damaged = True if details.condition in {
        "https://schema.org/DamagedCondition", "http://schema.org/DamagedCondition"
    } else None
    evidence = PublicEvidence(
        listing_id=candidate.listing_id,
        publication_proven=bool(identity_ok and date_ok and candidate.evidence == "public_card_add_date"),
        active=active,
        category=visible.category,
        price_usd=price,
        region=visible.region,
        year=details.year,
        # Observed live JSON-LD uses the category "Легкові" as bodyType.
        # This is unknown body style, not a contradiction with sedan/wagon.
        body=None if details.body in {"Легкові", "Легковые"} else details.body,
        fuel=details.fuel,
        transmission=details.transmission,
        mileage_km=_mileage(details.mileage_km),
        abroad=None,
        needs_customs=None,
        damaged=damaged,
        repair_parts=None,
        brand=details.brand,
        model=details.model,
    )
    return AdaptedEvidence(evidence, tuple(sorted(issues)), False)
