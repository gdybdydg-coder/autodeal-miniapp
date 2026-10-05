"""Offline detail-batch planner using only saved public search observations.

Title-derived values are *routing hints*, never vehicle attributes.  They may
order a bounded detail batch, but valuation still requires the full-page
corroboration performed by :mod:`vehicle_attributes` and :mod:`valuation`.
Price is deliberately absent from every ranking key.
"""
from __future__ import annotations

import hashlib
import re


MODIFICATIONS = (
    (r"(?<!\d)1[.,]6\s*(?:tdi|тді)(?!\w)", "1.6 tdi"),
    (r"(?<!\d)1[.,]6\s*(?:mpi|мпі)(?!\w)", "1.6 mpi"),
    (r"(?<!\d)1[.,]6\s*(?:fsi|фсі)(?!\w)", "1.6 fsi"),
    (r"(?<!\d)1[.,]8\s*(?:tsi|тсі)(?!\w)", "1.8 tsi"),
    (r"(?<!\d)1[.,]9\s*(?:tdi|тді)(?!\w)", "1.9 tdi"),
    (r"(?<!\d)2[.,]0\s*(?:tdi|тді)(?!\w)", "2.0 tdi"),
    (r"(?<!\d)2[.,]0\s*(?:fsi|фсі)(?!\w)", "2.0 fsi"),
)
BODY_HINTS = (
    (r"(?<!\w)(?:combi|комб[іи]|ун[іи]версал|universal|wagon)(?!\w)", "wagon"),
    (r"(?<!\w)(?:liftback|л[іи]фтбек)(?!\w)", "liftback"),
)
POWER = re.compile(r"(?<!\d)(\d{2,3})\s*(?:hp|к\.?\s*с\.?)\b", re.I)
SAFE_FIELDS = (
    "source", "id", "url", "price", "currency", "year", "mileage_km",
    "engine_cc", "fuel", "transmission", "body", "brand", "model",
    "checked_at", "first_seen_at", "observed_search_reason", "field_conflicts",
)


def title_hints(title: str) -> dict:
    """Return a fixed, non-reversible vocabulary; retain no seller text."""
    if not isinstance(title, str):
        raise TypeError("title must be text")
    folded = " ".join(title.casefold().split())
    modifications = {value for pattern, value in MODIFICATIONS if re.search(pattern, folded, re.I)}
    bodies = {value for pattern, value in BODY_HINTS if re.search(pattern, folded, re.I)}
    powers = {int(value) for value in POWER.findall(folded) if 40 <= int(value) <= 1000}
    return {
        "explicit_a5": bool(re.search(r"(?<!\w)a\s*5(?!\w)", folded, re.I)),
        "other_generation_or_tour": bool(re.search(r"(?<!\w)(?:a\s*[4678]|tour|тур)(?!\w)", folded, re.I)),
        "body_hint": next(iter(bodies)) if len(bodies) == 1 else None,
        "modification_hint": next(iter(modifications)) if len(modifications) == 1 else None,
        "power_hp_hint": next(iter(powers)) if len(powers) == 1 else None,
        "ambiguous_hint": len(bodies) > 1 or len(modifications) > 1 or len(powers) > 1,
    }


def sanitized_candidate(card: dict) -> dict:
    """Produce a repository-safe candidate without raw title/VIN/contact text."""
    title = card.get("title")
    result = {field: card.get(field) for field in SAFE_FIELDS}
    result["title_sha256"] = hashlib.sha256(title.encode()).hexdigest() if isinstance(title, str) else None
    result["title_hints"] = title_hints(title) if isinstance(title, str) else title_hints("")
    return result


def _valid(card: dict) -> bool:
    return (
        card.get("source") == "olx" and isinstance(card.get("id"), str)
        and isinstance(card.get("url"), str)
        and type(card.get("year")) is int and type(card.get("mileage_km")) is int
        and type(card.get("engine_cc")) is int
        and isinstance(card.get("fuel"), str) and isinstance(card.get("transmission"), str)
        and card.get("title_hints", {}).get("explicit_a5") is True
        and not card.get("title_hints", {}).get("other_generation_or_tour")
        and not card.get("title_hints", {}).get("ambiguous_hint")
    )


def _pool(seed: dict, candidates: list[dict]) -> list[dict]:
    wanted = seed["title_hints"].get("modification_hint")
    result = []
    for card in candidates:
        if not _valid(card):
            continue
        if (card["engine_cc"], card["fuel"], card["transmission"]) != (
            seed["engine_cc"], seed["fuel"], seed["transmission"]
        ):
            continue
        if abs(card["year"] - seed["year"]) > 1 or abs(card["mileage_km"] - seed["mileage_km"]) > 30000:
            continue
        actual = card["title_hints"].get("modification_hint")
        if actual is not None and actual != wanted:
            continue
        result.append(card)
    return result


def _rank(card: dict, modification: str) -> tuple:
    hints = card["title_hints"]
    # No price/currency: density, organic discovery and stable identity only.
    return (
        hints.get("modification_hint") == modification,
        hints.get("body_hint") is not None,
        hints.get("power_hp_hint") is not None,
        bool(hints.get("explicit_a5")),
        card.get("observed_search_reason") == "organic",
        card["source"], card["id"],
    )


def plan_detail_batch(candidates: list[dict], *, minimum: int = 8, max_calls: int = 20) -> dict:
    """Choose the densest compatible-hint window without asserting compatibility.

    The returned URLs are only a future bounded collection plan.  Distinct ad
    IDs are explicitly not counted as independently verified physical cars.
    """
    if type(minimum) is not int or minimum < 8:
        raise ValueError("minimum must stay at least 8")
    if type(max_calls) is not int or not minimum <= max_calls <= 20:
        raise ValueError("max_calls must be between minimum and 20")
    unique = {}
    for card in candidates:
        key = (card.get("source"), card.get("id"))
        if key not in unique:
            unique[key] = card
    rows = list(unique.values())
    seeds = [c for c in rows if _valid(c) and c["title_hints"].get("modification_hint")]
    options = []
    for seed in seeds:
        pool = _pool(seed, rows)
        modification = seed["title_hints"]["modification_hint"]
        exact = sum(c["title_hints"].get("modification_hint") == modification for c in pool)
        richness = sum(
            int(c["title_hints"].get("body_hint") is not None)
            + int(c["title_hints"].get("power_hp_hint") is not None)
            for c in pool
        )
        options.append((exact, len(pool), richness, seed["source"], seed["id"], seed, pool))
    if not options:
        return {
            "status": "insufficient_hint_density", "required_sample": minimum,
            "planned_calls": 0, "candidates": [], "price_used_for_selection": False,
            "distinct_ads_are_verified_physical_vehicles": False,
        }
    _, _, _, _, _, seed, pool = max(options, key=lambda option: option[:5])
    modification = seed["title_hints"]["modification_hint"]
    chosen = sorted(pool, key=lambda card: _rank(card, modification), reverse=True)[:max_calls]
    safe = [{
        "source": card["source"], "id": card["id"], "url": card["url"],
        "selection_hints": card["title_hints"],
    } for card in chosen]
    plannable = len(chosen) >= minimum
    return {
        "status": "detail_batch_plannable" if plannable else "insufficient_hint_density",
        "required_sample": minimum,
        "planned_calls": len(chosen) if plannable else 0,
        "candidate_window_count": len(pool),
        "exact_modification_hint_count": sum(
            card["title_hints"].get("modification_hint") == modification for card in pool
        ),
        "profile": {
            "engine_cc": seed["engine_cc"], "fuel": seed["fuel"],
            "transmission": seed["transmission"], "year_center": seed["year"],
            "mileage_km_center": seed["mileage_km"], "modification_hint": modification,
            "year_tolerance": 1, "mileage_km_tolerance": 30000,
        },
        "candidates": safe if plannable else [],
        "selection_hints_are_verified_attributes": False,
        "distinct_ads_are_verified_physical_vehicles": False,
        "price_used_for_selection": False,
        "remaining_detail_requirements": [
            "body", "drive_type", "power_hp", "research_condition",
            "full_page_asking_display", "vehicle_identity",
        ],
    }
