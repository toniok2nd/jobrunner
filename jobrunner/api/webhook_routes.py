"""Synchronous custom endpoints declared in job files with @endpoint("<path>").

    GET/POST /api/v1/web/<job>/<path> -> runs the handler in the worker
    GET      /api/v1/webhooks         -> lists every declared endpoint

Auth: same bearer tokens as the rest of the API.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from ..deps import optional_token, require_token
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
                    "public": ep.public,
                    "methods": sorted(ep.methods),
                    "url": f"/api/v1/web/{res.name}/{ep.path}",
                }
            )
    return sorted(out, key=lambda e: (e["job"], e["path"]))


@router.api_route("/web/{job_name}/{endpoint_path}", methods=["GET", "POST"])
async def call_webhook(
    job_name: str,
    endpoint_path: str,
    request: Request,
    token=Depends(optional_token),
):
    # Check auth before dispatching: endpoints are private unless the job
    # declared them with @endpoint(path, public=True).
    res = load_registry().get(job_name)
    ep = res.job.endpoints.get(endpoint_path) if res and res.job else None
    if ep is None:
        raise HTTPException(status_code=404, detail="endpoint not found")
    if request.method not in ep.methods:
        allowed = sorted(ep.methods)
        raise HTTPException(
            status_code=405,
            detail=f"method not allowed: this endpoint accepts "
                   f"{'/'.join(allowed)} only",
            headers={"Allow": ", ".join(allowed)},
        )
    if not ep.public and token is None:
        raise HTTPException(
            status_code=401,
            detail="authentication required (this endpoint is not public)",
        )

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
        "auth": f"token:{token.label}" if token else "public",
    }
    try:
        payload = await run_in_threadpool(
            dispatch_webhook, job_name, endpoint_path, params, None, meta
        )
    except WebhookError as exc:
        # don't leak the list of private endpoint paths to anonymous callers
        if token is None and exc.code == "endpoint_not_found":
            exc.payload.pop("available", None)
        raise _http_error(exc)

    # Handler returned a custom response (html(), text(), …)
    if "content_type" in payload:
        headers = dict(payload.get("headers") or {})
        headers["X-Jobrunner-Run-Id"] = payload["run_id"]
        return Response(
            content=payload["body"],
            media_type=payload["content_type"],
            status_code=payload.get("http_status", 200),
            headers=headers,
        )
    return payload
