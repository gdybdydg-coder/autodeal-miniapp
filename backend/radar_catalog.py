"""Persistent official AUTO.RIA identities; configuring radars never calls its API."""
import hashlib
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from .auto_ria import RiaError
from .models import SourceCache
from .radar_models import BrandCatalog, ModelCatalog, RegionCatalog, FuelCatalog

SEED_PATH = Path(__file__).with_name("data") / "radar_catalog_seed.json"
POPULARITY_PATH = Path(__file__).with_name("data") / "radar_brand_popularity.json"
FUEL_SEED = [{"id":1,"name":"Бензин"},{"id":2,"name":"Дизель"},{"id":3,"name":"Газ"},
    {"id":4,"name":"Газ пропан-бутан / Бензин"},{"id":5,"name":"Гібрид (HEV)"},
    {"id":6,"name":"Електро"},{"id":8,"name":"Газ метан / Бензин"},{"id":10,"name":"Гібрид (PHEV)"}]


@lru_cache(maxsize=1)
def brand_popularity():
    """Versioned public-selector snapshot; opening a Radar spends no API quota."""
    data = json.loads(POPULARITY_PATH.read_text(encoding="utf-8"))
    ids = data["brand_ids"]
    if not ids or any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Invalid bundled AUTO.RIA brand order")
    return {value: index + 1 for index, value in enumerate(ids)}, {
        "source_url": data["source_url"], "observed_at": data["observed_at"],
        "basis": data["basis"]}


def dictionary_key(path):
    return hashlib.sha256(json.dumps([path, {}], sort_keys=True).encode()).hexdigest()


def _items(payload):
    rows = payload.get("items", []) if isinstance(payload, dict) else []
    return [{"id": int(row["value"]), "name": row["name"].strip()} for row in rows
            if isinstance(row, dict) and str(row.get("value", "")).isdecimal()
            and 0 < int(row["value"]) < 2**31 and isinstance(row.get("name"), str)
            and 0 < len(row["name"].strip()) <= 150]


def import_catalog(db, payload):
    """Merge verified identity records, never delete referenced catalog entries."""
    db.info.pop("radar_catalog_resolver",None)
    # Versioned snapshots may flatten models to avoid duplicating catalog data.
    if "models" in payload:
        available=set(payload.get("model_catalogs",[]))
        grouped={}
        for item in payload["models"]:
            grouped.setdefault(item["brand_id"],[]).append({"id":item["id"],"name":item["name"]})
        payload={**payload,"brands":[{**entry,"models":grouped.get(entry["id"],[]),
            "models_available":entry["id"] in available or bool(grouped.get(entry["id"]))}
            for entry in payload.get("brands",[])]}
    brands = {row.id: row for row in db.scalars(select(BrandCatalog))}
    models = {(row.brand_id, row.id): row for row in db.scalars(select(ModelCatalog))}
    regions = {row.id: row for row in db.scalars(select(RegionCatalog))}
    fuels = {row.id: row for row in db.scalars(select(FuelCatalog))}
    for entry in payload.get("brands", []):
        ident, name = entry["id"], entry["name"]
        if type(ident) is not int or ident <= 0 or not isinstance(name, str) or not name.strip():
            raise ValueError("Некоректний довідник марок AUTO.RIA.")
        row = brands.get(ident)
        if row is None:
            row = BrandCatalog(id=ident, name=name, models_available=False)
            db.add(row)
            brands[ident] = row
        else:
            row.name = name
        row.models_available = row.models_available or entry.get("models_available", bool(entry.get("models")))
    db.flush()
    for entry in payload.get("brands", []):
        for item in entry.get("models", []):
            ident, name = item["id"], item["name"]
            if type(ident) is not int or ident <= 0 or not isinstance(name, str) or not name.strip():
                raise ValueError("Некоректний довідник моделей AUTO.RIA.")
            key = entry["id"], ident
            row = models.get(key)
            if row is None:
                row = ModelCatalog(brand_id=key[0], id=ident, name=name)
                db.add(row)
                models[key] = row
            else:
                row.name = name
    for entry in payload.get("regions", []):
        ident, name = entry["id"], entry["name"]
        if type(ident) is not int or ident <= 0 or not isinstance(name, str) or not name.strip():
            raise ValueError("Некоректний довідник областей AUTO.RIA.")
        if ident not in regions:
            regions[ident] = RegionCatalog(id=ident, name=name)
            db.add(regions[ident])
        else:
            regions[ident].name = name
    for entry in payload.get("fuels", []):
        ident, name = entry["id"], entry["name"]
        if type(ident) is not int or ident <= 0 or not isinstance(name, str) or not name.strip():
            raise ValueError("Некоректний довідник пального AUTO.RIA.")
        if ident not in fuels:
            fuels[ident] = FuelCatalog(id=ident, name=name)
            db.add(fuels[ident])
        else:
            fuels[ident].name = name
    db.flush()


def import_existing_cache(db):
    """Expired dictionaries remain valid identities; no prices or car data reused."""
    brand = db.get(SourceCache, dictionary_key("categories/1/marks"))
    states = db.get(SourceCache, dictionary_key("states"))
    fuel_cache = db.get(SourceCache, dictionary_key("type"))
    payload = {"brands": _items(brand.payload) if brand else [],
               "regions": _items(states.payload) if states else [], "fuels": FUEL_SEED}
    keys = {dictionary_key(f"categories/1/marks/{row['id']}/models"): row for row in payload["brands"]}
    if keys:
        for cached in db.scalars(select(SourceCache).where(SourceCache.id.in_(keys))):
            keys[cached.id]["models"] = _items(cached.payload)
            keys[cached.id]["models_available"] = True
    import_catalog(db, payload)
    if fuel_cache:
        import_catalog(db, {"fuels": _items(fuel_cache.payload)})
    if SEED_PATH.exists():
        import_catalog(db, json.loads(SEED_PATH.read_text(encoding="utf-8")))


def view(db, brand_id=None):
    # Existing deployments may already have the original Radar migration marker.
    # create_all adds the new tables, while this lazy seed fills their initial catalog.
    if db.scalar(select(FuelCatalog.id).limit(1)) is None:
        cached = db.get(SourceCache, dictionary_key("type"))
        import_catalog(db, {"fuels": _items(cached.payload) if cached else FUEL_SEED})
    ranks, order = brand_popularity()
    brands = [{"id": row.id, "name": row.name, "models_available": row.models_available}
              for row in db.scalars(select(BrandCatalog).order_by(BrandCatalog.name))]
    for brand in brands:
        brand["popularity_rank"] = ranks.get(brand["id"])
    # Preserve every catalog brand, including entries added after the snapshot.
    # Unranked entries follow the verified order, with a deterministic tie-break.
    brands.sort(key=lambda brand: (ranks.get(brand["id"], len(ranks) + 1), brand["name"].casefold(), brand["id"]))
    regions = [{"id": row.id, "name": row.name if row.name.endswith("область") or not row.name.endswith(("ська", "цька")) else row.name + " область"}
               for row in db.scalars(select(RegionCatalog).order_by(RegionCatalog.name))]
    models = []
    if brand_id is not None:
        models = [{"id": row.id, "brand_id": row.brand_id, "name": row.name}
                  for row in db.scalars(select(ModelCatalog).where(ModelCatalog.brand_id == brand_id).order_by(ModelCatalog.name))]
    fuels = [{"id": row.id, "name": row.name} for row in db.scalars(select(FuelCatalog).order_by(FuelCatalog.id))]
    return {"brands": brands, "brand_order": order, "regions": regions, "fuel_types": fuels, "models": models,
            "year_min": 1900, "year_max": min(2100, datetime.now().year + 1),
            "price_categories": [{"id": i, "minimum": low, "maximum_exclusive": high}
                for i, low, high in [(1,0,2000),(2,2000,5000),(3,5000,10000),(4,10000,15000),(5,15000,20000),(6,20000,None)]],
            "profit_percentages": [5,10,15,20,30,40,50], "source": "AUTO.RIA"}


def resolve(db, path, names):
    """Existing RiaSearch resolver contract, backed by persistent dictionaries."""
    from .ria_search import normalize
    if not names:
        return []
    transaction=db.get_transaction()
    cached=db.info.get("radar_catalog_resolver")
    paths=cached[1] if cached and cached[0] is transaction else {}
    if path not in paths:
        if path == "categories/1/marks":
            rows = db.execute(select(BrandCatalog.id, BrandCatalog.name)).all()
        elif path == "states":
            rows = db.execute(select(RegionCatalog.id, RegionCatalog.name)).all()
        elif path == "type":
            rows = db.execute(select(FuelCatalog.id, FuelCatalog.name)).all()
        else:
            match = re.fullmatch(r"categories/1/marks/([1-9][0-9]*)/models", path)
            if not match:
                raise RiaError("unsupported_filter")
            rows = db.execute(select(ModelCatalog.id, ModelCatalog.name).where(ModelCatalog.brand_id == int(match[1]))).all()
        mapping={}
        for ident,value in rows:
            mapping.setdefault(normalize(value.removesuffix(" область")),set()).add(ident)
        paths[path]=mapping
        # Session may have autobegun on the SELECT above. Never carry a catalog
        # snapshot across transaction boundaries or dictionary imports.
        db.info["radar_catalog_resolver"]=(db.get_transaction(),paths)
    result = []
    for name in names:
        matches = paths[path].get(normalize(name.removesuffix(" область")),set())
        if len(matches) != 1:
            raise RiaError("unsupported_filter")
        result.append(next(iter(matches)))
    return result
