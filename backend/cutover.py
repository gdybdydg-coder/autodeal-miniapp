"""Explicit deployment hold: HTTP readiness only, without application startup.

The existing service first runs this factory while the old binary drains. Only
the next deployment may enter normal startup and its versioned Radar migration.
"""
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def maintenance_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def hold(request, call_next):
        headers = {"Cache-Control": "no-store"}
        if request.method == "GET" and request.url.path == "/health":
            # Presence only: never echo credentials or open a database/provider
            # connection. Health here means maintenance HTTP readiness only.
            result = {"ok": True, "maintenance": True,
                "monitor_enabled": os.getenv("MONITOR_ENABLED") == "true",
                "delivery_enabled": os.getenv("DELIVERY_ENABLED") == "true",
                "source_ready": os.getenv("SOURCE_READY") == "true",
                "ria_ai_price_enabled": os.getenv("RIA_AI_PRICE_ENABLED") == "true",
                "auto_ria_api_key_configured": bool(os.getenv("AUTO_RIA_API_KEY", "").strip()),
                "auto_ria_user_id_configured": bool(os.getenv("AUTO_RIA_USER_ID", "").strip())}
            if os.getenv("RENDER_GIT_COMMIT"):
                result["release"] = os.environ["RENDER_GIT_COMMIT"]
            return JSONResponse(result, headers=headers)
        return JSONResponse({"detail": "Оновлюємо застосунок. Спробуйте ще раз за хвилину."},
                            status_code=503, headers={**headers, "Retry-After": "60"})

    return app
