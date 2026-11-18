"""Authenticated Radar routes using the existing Telegram identity dependency."""
from fastapi import Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import radar_catalog, radar_service as service
from .radar_models import BrandCatalog, Radar
from .models import User


def install(app, engine, identity):
    @app.exception_handler(service.RadarError)
    async def radar_error(request, exc):
        return JSONResponse({"detail":exc.message,"code":exc.code},status_code=exc.status)

    @app.get("/api/radars/catalog")
    def catalog(brand_id: int | None = Query(default=None,gt=0),uid=Depends(identity)):
        with Session(engine) as db:
            if brand_id is not None and db.get(BrandCatalog,brand_id) is None:
                raise service.RadarError(404,"brand_not_found","Марку не знайдено в довіднику AUTO.RIA.")
            result=radar_catalog.view(db,brand_id)
            if not result["brands"] or not result["regions"]:
                raise service.RadarError(503,"catalog_unavailable","Довідник марок та областей тимчасово недоступний. Спробуйте пізніше.")
            return result

    @app.get("/api/radars")
    def list_radars(uid=Depends(identity)):
        with Session(engine) as db,db.begin():
            # A read refresh must not rewrite the User row; lock only for a
            # coherent multi-table snapshot and a possible expiry transition.
            db.scalar(select(User).where(User.id==uid).with_for_update())
            from .radar_monitor import sync_expired
            sync_expired(db,uid=uid)
            rows=list(db.scalars(select(Radar).where(Radar.user_id==uid).order_by(Radar.slot)))
            return {"radars":service.configurations(db,rows),"limit":3,"access":service.access(db,uid)}

    @app.post("/api/radars",status_code=201)
    def create_radar(payload:service.RadarCreate,uid=Depends(identity)):
        with Session(engine) as db,db.begin():
            return service.create(db,uid,payload)

    @app.put("/api/radars/{radar_id}")
    def edit_radar(radar_id:str,payload:service.RadarEdit,uid=Depends(identity)):
        with Session(engine) as db,db.begin():
            return service.edit(db,uid,radar_id,payload)

    @app.patch("/api/radars/{radar_id}/status")
    def radar_status(radar_id:str,payload:service.RadarStatus,uid=Depends(identity)):
        with Session(engine) as db,db.begin():
            return service.set_status(db,uid,radar_id,payload)

    @app.delete("/api/radars/{radar_id}",status_code=204)
    def delete_radar(radar_id:str,revision:int=Query(ge=1),uid=Depends(identity)):
        with Session(engine) as db,db.begin():
            service.remove(db,uid,radar_id,revision)
        return Response(status_code=204)
