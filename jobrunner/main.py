from contextlib import asynccontextmanager
from pathlib import Path

import redis as redis_lib
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import job_routes, run_routes, webhook_routes
from .config import get_settings
from .db import init_db
from .deps import AuthRedirect
from .ui import routes as ui_routes


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="jobrunner",
    version="0.1.0",
    lifespan=lifespan,
    # /docs belongs to the in-app guide; Swagger/OpenAPI moves under /api
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/v1/openapi.json",
)

_static_dir = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

app.include_router(job_routes.router)
app.include_router(run_routes.router)
app.include_router(webhook_routes.router)
app.include_router(ui_routes.router)


@app.exception_handler(AuthRedirect)
async def _auth_redirect(_request: Request, exc: AuthRedirect):
    return RedirectResponse(exc.url, status_code=303)


@app.get("/api/v1/health", tags=["meta"])
def health():
    try:
        redis_lib.Redis.from_url(
            get_settings().redis_url, socket_timeout=2
        ).ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unreachable"
    return {
        "status": "ok" if redis_status == "ok" else "degraded",
        "redis": redis_status,
    }
