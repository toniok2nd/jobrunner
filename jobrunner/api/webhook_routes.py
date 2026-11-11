"""Synchronous custom endpoints declared in job files with @endpoint("<path>").

    GET/POST /api/v1/web/<job>/<path> -> runs the handler in the worker
    GET      /api/v1/webhooks         -> lists every declared endpoint

Auth: same bearer tokens as the rest of the API.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..deps import require_token
from ..loader import load_registry
from ..services import WebhookError, dispatch_webhook

router = APIRouter(prefix="/api/v1", tags=["webhooks"])

_STATUS_BY_CODE = {
    "job_not_found": 404,
    "endpoint_not_found": 404,
    "handler_error": 500,
    "timeout": 504,
}


def _http_error(exc: WebhookError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, 500), detail=exc.payload
    )


@router.get("/webhooks", dependencies=[Depends(require_token)])
def list_endpoints():
    registry = load_registry()
    out = []
    for res in registry.values():
        if res.job is None:
            continue
        for ep in res.job.endpoints.values():
            out.append(
                {
                    "job": res.name,
                    "path": ep.path,
                    "handler": ep.name,
                    "url": f"/api/v1/web/{res.name}/{ep.path}",
                }
            )
    return sorted(out, key=lambda e: (e["job"], e["path"]))


@router.api_route("/web/{job_name}/{endpoint_path}", methods=["GET", "POST"])
async def call_webhook(
    job_name: str,
    endpoint_path: str,
    request: Request,
    token=Depends(require_token),
):
    params: dict = {}
    if request.method == "POST":
        try:
            body = await request.json()
            params = body if isinstance(body, dict) else {"body": body}
        except Exception:
            pass  # empty or non-JSON body -> no params
    params.update(dict(request.query_params))

    meta = {
        "method": request.method,
        "url": str(request.url.path),
        "client": request.client.host if request.client else "-",
        "user_agent": request.headers.get("user-agent", "-"),
        # label of the API token only — the token itself is never logged
        "auth": f"token:{token.label}",
    }
    try:
        return await run_in_threadpool(
            dispatch_webhook, job_name, endpoint_path, params, None, meta
        )
    except WebhookError as exc:
        raise _http_error(exc)
