"""Ordering comes from a public AUTO.RIA snapshot, never a paid runtime call."""
import json
from pathlib import Path

from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import Session

from backend.models import Base
from backend import radar_catalog
from backend.radar_models import BrandCatalog


def test_bundled_popularity_uses_unique_official_identities():
    ranks,metadata=radar_catalog.brand_popularity()
    seed=json.loads(radar_catalog.SEED_PATH.read_text(encoding="utf-8"))
    known={row["id"] for row in seed["brands"]}
    assert len(ranks)>=30
    assert set(ranks)<=known
    assert sorted(ranks.values())==list(range(1,len(ranks)+1))
    assert metadata["source_url"].startswith("https://auto.ria.com/")
    assert metadata["observed_at"]


def test_catalog_preserves_provider_order_and_unranked_brands_without_api(tmp_path):
    ranks,_=radar_catalog.brand_popularity()
    first,second=list(ranks)[:2]
    engine=create_engine("sqlite:///"+str(tmp_path/"order.sqlite3"))
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db,db.begin():
            radar_catalog.import_catalog(db,{"brands":[
                {"id":first,"name":"Zulu first by popularity"},
                {"id":second,"name":"Alpha second by popularity"},
                {"id":2147483001,"name":"Zzz new brand"},
                {"id":2147483000,"name":"Aaa new brand"}],"regions":[]})
        with Session(engine) as db:
            result=radar_catalog.view(db)
            assert [row["id"] for row in result["brands"]]==[first,second,2147483000,2147483001]
            assert [row["popularity_rank"] for row in result["brands"]]==[1,2,None,None]
            assert db.scalar(select(func.count()).select_from(BrandCatalog))==4
            assert radar_catalog.view(db,first)["brands"]==result["brands"]
    finally:engine.dispose()
