"""Offline parser and publication-proof state for public listing cards.

No network, database, backend, credentials or delivery integration. A selected
candidate is only eligible for a later detail check; it is never delivery proof.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from html.parser import HTMLParser
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
MAX_HTML = 2_500_000
MAX_CARDS = 200
MAX_AGE = 3600
PROMOTED_CLASSES = frozenset({"paid", "sponsored", "native-ad", "ticket-item--advert", "ticket-item--paid"})


class CardParseError(ValueError):
    """Stable reason code; never contains source HTML or seller data."""


@dataclass(frozen=True)
class PublicCard:
    listing_id: str
    url: str | None
    added_at: float | None
    updated_at: float | None
    preview_usd: Decimal | None
    promoted: bool
    issues: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Candidate:
    listing_id: str
    url: str
    added_at: float
    preview_usd: Decimal | None
    evidence: str = "public_card_add_date"


@dataclass(frozen=True)
class PublicationState:
    baseline_at: float | None
    seen_additions: tuple[tuple[str, float], ...] = ()

    def as_dict(self):
        return {"baseline_at": self.baseline_at, "seen_additions": dict(self.seen_additions)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise CardParseError("invalid_state")
        baseline = value.get("baseline_at")
        seen = value.get("seen_additions", {})
        if baseline is not None and not _finite_time(baseline):
            raise CardParseError("invalid_state")
        if not isinstance(seen, dict) or len(seen) > 10000:
            raise CardParseError("invalid_state")
        rows = []
        for sid, added in seen.items():
            if not isinstance(sid, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", sid) or not _finite_time(added):
                raise CardParseError("invalid_state")
            rows.append((sid, float(added)))
        return cls(None if baseline is None else float(baseline), tuple(sorted(rows)))


def source_time(value):
    """Parse explicit/UTC timestamps; naive values mean Kyiv and DST ambiguity fails."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
                return None
            early, late = stamp.replace(tzinfo=KYIV, fold=0), stamp.replace(tzinfo=KYIV, fold=1)
            if early.utcoffset() != late.utcoffset():
                return None
            if datetime.fromtimestamp(early.timestamp(), KYIV).replace(tzinfo=None) != stamp:
                return None
            stamp = early
        result = stamp.timestamp()
        return result if _finite_time(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def _finite_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _price(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() and result > 0 else None


def _listing_url(value, listing_id):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc != "auto.ria.com" or url.query or url.fragment:
        return None
    if not re.fullmatch(r"/uk/auto_[A-Za-z0-9_-]+_" + re.escape(listing_id) + r"\.html", url.path):
        return None
    return value


class _Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.current, self.section_depth, self.count = [], None, 0, 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        classes = set((data.get("class") or "").split())
        if tag == "section":
            if "ticket-item" in classes:
                if self.current is not None or self.count >= MAX_CARDS:
                    raise CardParseError("unexpected_structure")
                self.count += 1
                self.current = {"id": data.get("data-advertisement-id"), "add": [], "update": [],
                                "links": [], "prices": [], "promoted": False}
                self.section_depth = 1
            elif self.current is not None:
                self.section_depth += 1
        if self.current is None:
            return
        if classes & PROMOTED_CLASSES or any(data.get(k) in ("true", "1") for k in
                                             ("data-sponsored", "data-is-advert", "data-is-paid")):
            self.current["promoted"] = True
        if "data-add-date" in data:
            self.current["add"].append(source_time(data["data-add-date"]))
        if "data-update-date" in data:
            self.current["update"].append(source_time(data["data-update-date"]))
        if tag == "a" and "m-link-ticket" in classes:
            self.current["links"].append(data.get("href"))
        if "price-ticket" in classes and data.get("data-main-currency") == "USD":
            self.current["prices"].append(_price(data.get("data-main-price")))

    def handle_endtag(self, tag):
        if tag == "section" and self.current is not None:
            self.section_depth -= 1
            if self.section_depth < 0:
                raise CardParseError("unexpected_structure")
            if self.section_depth == 0:
                self.rows.append(self.current)
                self.current = None


def parse_public_cards(html):
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML:
        raise CardParseError("html_oversize")
    parser = _Cards()
    parser.feed(html)
    if parser.current is not None or not 1 <= parser.count <= MAX_CARDS or len(parser.rows) != parser.count:
        raise CardParseError("unexpected_structure")
    result, ids = [], set()
    for row in parser.rows:
        sid = row["id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", sid) or sid in ids:
            raise CardParseError("invalid_or_duplicate_id")
        ids.add(sid)
        issues = set()
        add = row["add"][0] if len(row["add"]) == 1 else None
        if len(row["add"]) != 1 or add is None:
            issues.add("missing_or_invalid_add_date")
        update = row["update"][0] if len(row["update"]) == 1 else None
        if len(row["update"]) > 1 or (row["update"] and update is None):
            issues.add("invalid_update_date")
        link = _listing_url(row["links"][0], sid) if len(row["links"]) == 1 else None
        if link is None:
            issues.add("missing_or_invalid_url")
        price = row["prices"][0] if len(row["prices"]) == 1 else None
        if len(row["prices"]) > 1 or (row["prices"] and price is None):
            issues.add("invalid_or_conflicting_preview_price")
        if row["promoted"]:
            issues.add("promoted")
        provenance = [("added_at", "public_card_data_add_date")]
        if update is not None:
            provenance.append(("updated_at", "public_card_data_update_date"))
        if price is not None:
            provenance.append(("preview_usd", "public_card_preview"))
        result.append(PublicCard(sid, link, add, update, price, row["promoted"],
                                 tuple(sorted(issues)), tuple(provenance)))
    return tuple(result)


def advance_publications(state, cards, observed_at, max_age=MAX_AGE):
    """Return (new_state, candidates); first successful snapshot is baseline only."""
    if not isinstance(state, PublicationState) or not _finite_time(observed_at):
        raise CardParseError("invalid_state_or_observation")
    if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age <= 0:
        raise CardParseError("invalid_max_age")
    if not isinstance(cards, (tuple, list)) or len(cards) > MAX_CARDS or any(not isinstance(c, PublicCard) for c in cards):
        raise CardParseError("invalid_cards")
    if state.baseline_at is None:
        return PublicationState(float(observed_at), ()), ()
    if observed_at < state.baseline_at:
        raise CardParseError("clock_regression")
    candidates = []
    floor = max(state.baseline_at, observed_at - max_age)
    # Expired events cannot become candidates again; delivery dedupe is separate.
    seen = {sid: added for sid, added in state.seen_additions if added > floor}
    for card in sorted(cards, key=lambda c: (-(c.added_at or 0), c.listing_id)):
        blockers = set(card.issues) - {"invalid_update_date", "invalid_or_conflicting_preview_price"}
        if blockers or card.url is None or card.added_at is None:
            continue
        # update date, preview price and first observation are deliberately ignored as publication proof.
        if not floor < card.added_at <= observed_at:
            continue
        previous = seen.get(card.listing_id)
        if previous is not None and card.added_at <= previous:
            continue
        seen[card.listing_id] = card.added_at
        candidates.append(Candidate(card.listing_id, card.url, card.added_at, card.preview_usd))
    if len(seen) > 10000:
        raise CardParseError("publication_capacity")
    return PublicationState(state.baseline_at, tuple(sorted(seen.items()))), tuple(candidates)
