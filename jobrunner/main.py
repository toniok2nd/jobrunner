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
    # /docs belongs to the in-app guide; Swagger/OpenAPI moves under /api.
    # Built-in docs disabled: we serve Swagger UI from vendored assets (offline).
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
)


@app.get("/api/docs", include_in_schema=False)
def swagger_ui():
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(
        openapi_url="/api/v1/openapi.json",
        title="jobrunner API",
        swagger_js_url="/static/vendor/swagger-ui-bundle.js",
        swagger_css_url="/static/vendor/swagger-ui.css",
        swagger_favicon_url="/static/icon.svg",
    )


@app.get("/api/redoc", include_in_schema=False)
def redoc_ui():
    # Hand-written: fastapi's get_redoc_html hardcodes a Google Fonts <link>,
    # which breaks offline deployments. ReDoc works fine with system fonts.
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        """<!doctype html>
<html>
<head>
  <title>jobrunner API – ReDoc</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" type="image/svg+xml" href="/static/icon.svg">
  <style>body { margin: 0; padding: 0; }</style>
</head>
<body>
  <redoc spec-url="/api/v1/openapi.json"></redoc>
  <script src="/static/vendor/redoc.standalone.js"></script>
</body>
</html>"""
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


def _custom_openapi():
    """Add x-logo pointing at the local icon — otherwise ReDoc fetches its
    default logo from cdn.redoc.ly (breaks fully-offline deployments)."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title, version=app.version, routes=app.routes
    )
    schema["info"]["x-logo"] = {"url": "/static/icon.svg"}
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


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
