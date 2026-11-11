"""Server-rendered web UI. All pages require a session except /login."""

import inspect
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import logcapture, packages as pkgs
from ..config import get_settings
from ..deps import AuthRedirect, get_db, require_user
from ..loader import load_registry
from ..models import ACTIVE_STATUSES, ApiToken, JobRun, User
from ..security import verify_password
from ..services import (
    JobValidationError,
    WebhookError,
    cancel_run,
    create_token,
    delete_endpoint_from_file,
    delete_job_file,
    delete_shared_lib,
    dispatch_webhook,
    revoke_token,
    save_job_code,
    trigger_run,
    validate_job_name,
    validate_python_source,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- auth ---------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        "login.html", {"request": request, "user": None, "error": error}
    )


@router.post("/login")
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from ..security import SESSION_COOKIE, create_session_cookie

    user = db.scalar(select(User).where(User.username == username))
    response = _redirect("/")
    if user is None or not verify_password(password, user.password_hash):
        response = _redirect("/login?error=1")
        return response
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(username),
        httponly=True,
        samesite="lax",
        max_age=get_settings().session_max_age_seconds,
    )
    return response


@router.post("/logout")
def logout():
    from ..security import SESSION_COOKIE

    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- jobs ---------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    registry = load_registry()
    jobs = []
    for res in registry.values():
        last_run = db.scalar(
            select(JobRun)
            .where(JobRun.job_name == res.name)
            .order_by(JobRun.queued_at.desc())
            .limit(1)
        )
        jobs.append({"res": res, "last_run": last_run})
    return templates.TemplateResponse(
        "jobs.html", {"request": request, "user": user, "jobs": jobs}
    )


@router.get("/jobs/{name}", response_class=HTMLResponse)
def job_detail_page(
    name: str,
    request: Request,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="job not found")
    res = load_registry().get(name)
    if res is None:
        raise HTTPException(status_code=404, detail="job not found")
    runs = db.scalars(
        select(JobRun)
        .where(JobRun.job_name == name)
        .order_by(JobRun.queued_at.desc())
        .limit(10)
    ).all()
    endpoint_rows = []
    if res.job:
        for ep in res.job.endpoint_spec():
            endpoint_rows.append(
                {**ep, "curl": _curl_web_cmd(request, name, ep["path"])}
            )
    return templates.TemplateResponse(
        "job_detail.html",
        {"request": request, "user": user, "res": res, "runs": runs,
         "endpoint_rows": endpoint_rows},
    )


@router.post("/jobs/{name}/trigger")
def job_trigger(
    name: str,
    params: str = Form(""),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    res = load_registry().get(name)
    if res is None or res.job is None:
        raise HTTPException(status_code=404, detail="job not found or failed to load")
    try:
        parsed = json.loads(params) if params.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("params must be a JSON object")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid params JSON: {exc}")
    run = trigger_run(db, res.job, parsed, triggered_by="ui")
    return _redirect(f"/runs/{run.id}")


@router.get("/jobs/{name}/edit", response_class=HTMLResponse)
def editor_page(
    name: str,
    request: Request,
    user: str = Depends(require_user),
):
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="job not found")
    res = load_registry().get(name)
    source = res.source if res else (
        '"""New job."""\nfrom jobrunner.dsl import task\n\n\n@task\ndef main(ctx):\n    print("hello")\n'
    )
    return templates.TemplateResponse(
        "editor.html",
        {"request": request, "user": user, "name": name, "source": source},
    )


@router.post("/jobs/{name}/code")
async def editor_save(
    name: str,
    request: Request,
    user: str = Depends(require_user),
):
    body = await request.json()
    source = body.get("source", "")
    try:
        save_job_code(name, source)
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    res = load_registry().get(name)
    return {"saved": True, "load_error": res.error if res else None}


# --- go-to-definition for the editor -------------------------------------

_LIBRARY_MODULES = None


def _library_modules():
    """Package modules searchable by the editor's go-to-definition."""
    global _LIBRARY_MODULES
    if _LIBRARY_MODULES is None:
        from .. import dsl, executor, loader, logcapture, models, scheduler

        _LIBRARY_MODULES = [dsl, executor, models, logcapture, loader, scheduler]
    return _LIBRARY_MODULES


def _find_def_line(source: str, name: str) -> int | None:
    """1-based line of `def name` / `class name` / `name =` in source, if any."""
    def_pat = re.compile(rf"^\s*(?:def|class)\s+{re.escape(name)}\b")
    assign_pat = re.compile(rf"^\s*{re.escape(name)}\s*=")
    for idx, line in enumerate(source.splitlines()):
        if def_pat.match(line) or assign_pat.match(line):
            return idx + 1
    return None


def _find_in_jobrunner_package(name: str) -> dict | None:
    for module in _library_modules():
        path = Path(inspect.getsourcefile(module))
        source = path.read_text(encoding="utf-8")
        line = _find_def_line(source, name)
        if line:
            return {
                "filename": f"{module.__name__}.py",
                "source": source,
                "line": line,
                "module": module.__name__,
            }
    return None


def _render_builtin_stub(module, modname: str, attrs: list) -> dict | None:
    """For C/built-in modules (no .py source): render a docstring + member list."""
    if attrs:
        target = module
        for attr in attrs:
            target = getattr(target, attr, None)
            if target is None:
                break
        if target is not None and target is not module:
            try:
                sig = str(inspect.signature(target))
            except (TypeError, ValueError):
                sig = "(...)" if callable(target) else ""
            lines = [f"# {modname}.{'.'.join(attrs)} — built-in (no Python source)", ""]
            lines.append(f"{modname}.{'.'.join(attrs)}{sig}")
            doc = inspect.getdoc(target)
            if doc:
                lines += ["", inspect.cleandoc(doc)]
            return {
                "filename": f"{modname} (built-in)",
                "source": "\n".join(lines),
                "line": 1,
                "module": modname,
            }

    lines = [f"# module '{modname}' — built-in / C extension (no Python source)", ""]
    doc = inspect.getdoc(module)
    if doc:
        lines += doc.splitlines()
        lines.append("")
    lines.append(f"# Exported members of {modname}:")
    member_lines: dict[str, int] = {}
    for m in sorted(dir(module)):
        if m.startswith("_"):
            continue
        obj = getattr(module, m, None)
        if callable(obj):
            try:
                sig = str(inspect.signature(obj))
            except (TypeError, ValueError):
                sig = "(...)"
            line = f"def {m}{sig}"
        else:
            line = f"{m} = <{type(obj).__name__}>"
        member_lines[m] = len(lines) + 1
        lines.append(line)
    target_line = member_lines.get(attrs[-1], 1) if attrs else 1
    return {
        "filename": f"{modname} (built-in)",
        "source": "\n".join(lines),
        "line": target_line,
        "module": modname,
    }


def _resolve_env_symbol(symbol: str) -> dict | None:
    """Resolve `symbol` in the current Python environment (stdlib + installed
    venv packages), walking dotted paths like 'json.tool' or 'time.sleep'."""
    import importlib
    import importlib.util

    parts = symbol.split(".")
    for i in range(len(parts), 0, -1):
        modname = ".".join(parts[:i])
        try:
            spec = importlib.util.find_spec(modname)
        except Exception:
            spec = None
        if spec is None:
            continue
        origin = spec.origin or ""
        if origin.endswith(".py") and Path(origin).is_file():
            source = Path(origin).read_text(encoding="utf-8", errors="replace")
            line = 1
            if i < len(parts):
                line = _find_def_line(source, parts[i]) or 1
            return {
                "filename": f"{modname} — {origin}",
                "source": source,
                "line": line,
                "module": modname,
            }
        # built-in / frozen / C extension: no source file, render a stub
        try:
            module = importlib.import_module(modname)
        except Exception:
            continue
        return _render_builtin_stub(module, modname, parts[i:])
    return None


@router.get("/jobs/{name}/libsrc")
def library_source(
    name: str,
    symbol: str,
    user: str = Depends(require_user),
):
    """Locate `symbol`'s definition: jobrunner package first, then the current
    Python environment (stdlib + installed packages). Read-only."""
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="job not found")
    if not re.match(r"^[A-Za-z_][\w.]*$", symbol):
        raise HTTPException(status_code=400, detail="invalid symbol")

    if "." not in symbol:
        hit = _find_in_jobrunner_package(symbol)
        if hit:
            return hit

    hit = _resolve_env_symbol(symbol)
    if hit:
        return hit

    raise HTTPException(
        status_code=404, detail=f"no definition found for '{symbol}'"
    )


# --- shared libraries (jobs/lib/*.py, importable from jobs & endpoints) ----

_LIB_TEMPLATE = '''"""Module partagé — importable depuis les jobs et les endpoints :

    from {name} import hello
"""


def hello(name: str = "world") -> str:
    """Exemple de fonction partagée."""
    return f"hello {{name}}"
'''


def _libs_dir() -> Path:
    d = Path(get_settings().jobs_dir) / "lib"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_libs() -> list[dict]:
    from datetime import datetime

    return [
        {
            "name": p.stem,
            "size": p.stat().st_size,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for p in sorted(_libs_dir().glob("*.py"))
        if not p.name.startswith("_")
    ]


def _check_lib_import(name: str, source: str) -> str | None:
    """Exec the module source in a scratch namespace; returns an error string
    if it fails (catches bad imports / top-level errors, not just syntax)."""
    import types
    from time import time_ns

    module = types.ModuleType(f"_libcheck_{name}_{time_ns()}")
    module.__file__ = f"lib/{name}.py"
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


@router.get("/libs", response_class=HTMLResponse)
def libs_page(request: Request, user: str = Depends(require_user)):
    return templates.TemplateResponse(
        "libs.html",
        {"request": request, "user": user, "libs": _list_libs()},
    )


@router.post("/libs/create")
def libs_create(name: str = Form(...), user: str = Depends(require_user)):
    name = name.strip()
    try:
        validate_job_name(name)
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    path = _libs_dir() / f"{name}.py"
    if path.exists():
        raise HTTPException(status_code=409, detail=f"module '{name}' already exists")
    path.write_text(_LIB_TEMPLATE.format(name=name), encoding="utf-8")
    return _redirect(f"/libs/{name}/edit")


@router.get("/libs/{name}/edit", response_class=HTMLResponse)
def libs_edit_page(name: str, request: Request, user: str = Depends(require_user)):
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="module not found")
    path = _libs_dir() / f"{name}.py"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="module not found")
    return templates.TemplateResponse(
        "editor.html",
        {
            "request": request,
            "user": user,
            "name": name,
            "source": path.read_text(encoding="utf-8"),
            "save_url": f"/libs/{name}/code",
            "back_url": "/libs",
        },
    )


@router.post("/libs/{name}/code")
async def libs_save(name: str, request: Request, user: str = Depends(require_user)):
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="module not found")
    body = await request.json()
    source = body.get("source", "")
    try:
        validate_python_source(source)
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    load_error = _check_lib_import(name, source)
    path = _libs_dir() / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return {"saved": True, "load_error": load_error}


# --- deletion (jobs / endpoints / libs) ------------------------------------


@router.post("/jobs/{name}/delete")
def job_delete(name: str, user: str = Depends(require_user)):
    try:
        delete_job_file(name)
    except JobValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _redirect("/")


@router.post("/endpoints/{job}/{path}/delete")
def endpoint_delete(job: str, path: str, user: str = Depends(require_user)):
    try:
        delete_endpoint_from_file(job, path)
    except JobValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _redirect("/endpoints")


@router.post("/libs/{name}/delete")
def lib_delete(name: str, user: str = Depends(require_user)):
    try:
        delete_shared_lib(name)
    except JobValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _redirect("/libs")


# --- runs ---------------------------------------------------------------


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    job: str | None = None,
    status: str | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    stmt = select(JobRun).order_by(JobRun.queued_at.desc()).limit(100)
    if job:
        stmt = stmt.where(JobRun.job_name == job)
    if status:
        stmt = stmt.where(JobRun.status == status)
    runs = db.scalars(stmt).all()
    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "user": user,
            "runs": runs,
            "filter_job": job or "",
            "filter_status": status or "",
        },
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(
    run_id: str,
    request: Request,
    task: str | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    log = ""
    if task:
        path = logcapture.log_path(get_settings().data_dir, run_id, task)
        if path:
            log = path.read_text(encoding="utf-8", errors="replace")
    active = run.status in ACTIVE_STATUSES
    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "user": user,
            "run": run,
            "active": active,
            "selected_task": task,
            "log": log,
        },
    )


@router.get("/runs/{run_id}/frag/tasks", response_class=HTMLResponse)
def run_tasks_fragment(
    run_id: str,
    request: Request,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return templates.TemplateResponse(
        "partials/tasks.html",
        {"request": request, "run": run, "active": run.status in ACTIVE_STATUSES},
    )


@router.get("/runs/{run_id}/frag/log", response_class=HTMLResponse)
def run_log_fragment(
    run_id: str,
    request: Request,
    task: str | None = None,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    path = logcapture.log_path(get_settings().data_dir, run_id, task)
    log = path.read_text(encoding="utf-8", errors="replace") if path else "(no logs yet)"
    return templates.TemplateResponse(
        "partials/log.html",
        {"request": request, "log": log, "active": run.status in ACTIVE_STATUSES},
    )


@router.post("/runs/{run_id}/cancel")
def run_cancel(
    run_id: str,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    cancel_run(db, run)
    return _redirect(f"/runs/{run_id}")


# --- attribute/method completion for the editor ---------------------------

_CTX_MEMBERS = [
    {"name": "params", "kind": "data", "signature": None,
     "doc": "run parameters (default_params merged with trigger params)"},
    {"name": "run_id", "kind": "data", "signature": None,
     "doc": "id of the current run"},
    {"name": "attempt", "kind": "data", "signature": None,
     "doc": "attempt number of the current run"},
    {"name": "log", "kind": "function", "signature": "(message)",
     "doc": "write a line to this task's own log file"},
]


def _attr_kind(obj) -> str:
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        return "class"
    if callable(obj):
        return "function"
    return "data"


def _member_info(parent, name: str) -> dict:
    obj = getattr(parent, name)
    entry = {"name": name, "kind": _attr_kind(obj), "signature": None, "doc": None}
    if callable(obj):
        try:
            sig = str(inspect.signature(obj))
            entry["signature"] = sig if len(sig) <= 70 else sig[:67] + "…"
        except (TypeError, ValueError):
            entry["signature"] = "(…)"
    doc = inspect.getdoc(obj)
    if doc:
        entry["doc"] = doc.splitlines()[0][:90]
    return entry


def _resolve_object(path: str):
    """Import the longest importable prefix, then walk attributes."""
    import importlib

    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        modname = ".".join(parts[:i])
        try:
            module = importlib.import_module(modname)
        except Exception:
            continue
        obj = module
        for attr in parts[i:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
        return obj
    return None


@router.get("/jobs/{name}/pyattrs")
def python_attrs(
    name: str,
    symbol: str,
    user: str = Depends(require_user),
):
    """Members (methods/attributes) of a dotted path for editor completion,
    e.g. symbol=ctx -> ctx.log/params/..., symbol=time -> module members."""
    try:
        validate_job_name(name)
    except JobValidationError:
        raise HTTPException(status_code=404, detail="job not found")
    if not re.match(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$", symbol):
        raise HTTPException(status_code=400, detail="invalid symbol")

    if symbol == "ctx":
        return {"symbol": symbol, "members": _CTX_MEMBERS}

    obj = _resolve_object(symbol)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"cannot resolve '{symbol}'")

    members = [
        _member_info(obj, m)
        for m in sorted(dir(obj))
        if not m.startswith("_")
    ][:400]
    return {"symbol": symbol, "members": members}


# --- packages (pip into data/pythonlibs) ----------------------------------


@router.get("/packages", response_class=HTMLResponse)
def packages_page(
    request: Request,
    op: str | None = None,
    user: str = Depends(require_user),
):
    return templates.TemplateResponse(
        "packages.html",
        {
            "request": request,
            "user": user,
            "extra": pkgs.list_extra_packages(),
            "base": pkgs.list_base_packages(),
            "op": op,
            "op_state": pkgs.get_op(op) if op else None,
            "lib_dir": str(pkgs.extra_lib_dir()),
            "pip_cfg": pkgs.get_pip_config(),
            "mask": pkgs.mask_credentials,
        },
    )


@router.post("/packages/install")
def packages_install(
    spec: str = Form(...),
    user: str = Depends(require_user),
):
    try:
        op_id = pkgs.start_install(spec)
    except pkgs.PackageSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _redirect(f"/packages?op={op_id}")


@router.post("/packages/settings")
def packages_settings_save(
    index_url: str = Form(""),
    proxy: str = Form(""),
    user: str = Depends(require_user),
):
    try:
        pkgs.set_pip_config(index_url, proxy)
    except pkgs.PackageSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _redirect("/packages")


@router.get("/packages/op/{op_id}/frag", response_class=HTMLResponse)
def packages_op_fragment(
    op_id: str,
    request: Request,
    user: str = Depends(require_user),
):
    op_state = pkgs.get_op(op_id)
    if op_state is None:
        raise HTTPException(status_code=404, detail="operation not found")
    return templates.TemplateResponse(
        "partials/pip_op.html", {"request": request, "op": op_state}
    )


@router.post("/packages/uninstall")
def packages_uninstall(
    dist_info: str = Form(...),
    user: str = Depends(require_user),
):
    try:
        removed = pkgs.uninstall(dist_info)
    except pkgs.PackageSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _redirect("/packages")


# --- endpoints management (create / edit / test from the GUI) ---------------

_NEW_ENDPOINT_TEMPLATE = '''"""Endpoint file."""
from jobrunner.dsl import endpoint


@endpoint("{path}")
def {fn_name}(ctx):
    ctx.log(f"params: {{ctx.params}}")
    return {{"ok": True, "endpoint": "{path}"}}
'''

_APPEND_ENDPOINT_TEMPLATE = '''

@endpoint("{path}")
def {fn_name}(ctx):
    ctx.log(f"params: {{ctx.params}}")
    return {{"ok": True, "endpoint": "{path}"}}
'''


def _ensure_dsl_import(source: str) -> str:
    """Make sure the file imports `endpoint` from jobrunner.dsl."""
    import_line = re.compile(r"^from jobrunner\.dsl import (.+)$", re.M)
    m = import_line.search(source)
    if m:
        names = {n.strip() for n in m.group(1).split(",")}
        if "endpoint" in names:
            return source
        return import_line.sub(
            f"from jobrunner.dsl import {m.group(1).rstrip()}, endpoint", source, count=1
        )
    return "from jobrunner.dsl import endpoint\n\n" + source.lstrip("\n")


def _unique_fn_name(source: str, base: str) -> str:
    candidate, i = f"{base}_handler", 2
    while re.search(rf"^def {re.escape(candidate)}\b", source, re.M):
        candidate, i = f"{base}_handler_{i}", i + 1
    return candidate


def _curl_web_cmd(request: Request, job: str, path: str) -> str:
    return (
        f'curl -X POST {request.base_url}api/v1/web/{job}/{path} '
        f'-H "Authorization: Bearer jr_..." '
        f'-H "Content-Type: application/json" '
        f"-d '{{\"params\": {{}}}}'"
    )


@router.get("/endpoints", response_class=HTMLResponse)
def endpoints_page(request: Request, user: str = Depends(require_user)):
    registry = load_registry()
    endpoints = []
    for res in registry.values():
        if res.job:
            for ep in res.job.endpoints.values():
                endpoints.append(
                    {"job": res.name, "path": ep.path, "handler": ep.name,
                     "public": ep.public, "methods": sorted(ep.methods),
                     "url": f"/api/v1/web/{res.name}/{ep.path}",
                     "curl": _curl_web_cmd(request, res.name, ep.path)}
                )
    return templates.TemplateResponse(
        "endpoints.html",
        {
            "request": request,
            "user": user,
            "endpoints": sorted(endpoints, key=lambda e: (e["job"], e["path"])),
            "job_files": [r.name for r in registry.values() if r.job or r.error is None],
        },
    )


@router.post("/endpoints/create")
def endpoints_create(
    file: str = Form(...),
    path: str = Form(...),
    user: str = Depends(require_user),
):
    from ..dsl import _ENDPOINT_PATH_RE

    file = file.strip()
    path = path.strip()
    try:
        validate_job_name(file)
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not _ENDPOINT_PATH_RE.match(path):
        raise HTTPException(
            status_code=400,
            detail="invalid endpoint path: letters, digits, - and _ only",
        )

    registry = load_registry()
    res = registry.get(file)
    if res is not None:
        if res.error:
            raise HTTPException(
                status_code=400,
                detail=f"file '{file}.py' has load errors — fix it in the editor first: {res.error}",
            )
        if path in res.job.endpoints:
            raise HTTPException(
                status_code=409,
                detail=f"'{file}.py' already has an endpoint '{path}'",
            )

    jobs_dir = Path(get_settings().jobs_dir)
    file_path = jobs_dir / f"{file}.py"
    base = path.replace("-", "_")
    old_source: str | None = None
    if file_path.exists():
        old_source = file_path.read_text(encoding="utf-8")
        new_source = _ensure_dsl_import(old_source) + _APPEND_ENDPOINT_TEMPLATE.format(
            path=path, fn_name=_unique_fn_name(old_source, base)
        )
    else:
        new_source = _NEW_ENDPOINT_TEMPLATE.format(path=path, fn_name=f"{base}_handler")
    try:
        save_job_code(file, new_source)  # validates job name + syntax
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = load_registry().get(file)
    if result is not None and result.error:
        # never leave a previously-working file broken by the append
        if old_source is not None:
            file_path.write_text(old_source, encoding="utf-8")
        raise HTTPException(status_code=400, detail=result.error)
    return _redirect(f"/jobs/{file}/edit")


@router.api_route("/endpoints/{job}/{path}/call", methods=["GET", "POST"])
async def endpoints_call(
    job: str,
    path: str,
    request: Request,
    user: str = Depends(require_user),
):
    """Quick-test an endpoint from the GUI (session auth, no token needed).
    POST: JSON body {"params": {...}} — GET: plain query string."""
    if request.method == "POST":
        body = await request.json()
        params = body.get("params", {})
        if not isinstance(params, dict):
            params = {"body": params}
    else:
        params = dict(request.query_params)
    from starlette.concurrency import run_in_threadpool

    meta = {
        "method": request.method,
        "url": f"/endpoints/{job}/{path}/call",
        "client": request.client.host if request.client else "-",
        "user_agent": request.headers.get("user-agent", "-"),
        "auth": f"session:{user}",
    }
    try:
        payload = await run_in_threadpool(dispatch_webhook, job, path, params, None, meta)
    except WebhookError as exc:
        status = {"job_not_found": 404, "endpoint_not_found": 404,
                  "timeout": 504}.get(exc.code, 500)
        raise HTTPException(status_code=status, detail=exc.payload)
    if "content_type" in payload:  # html()/text() responses from the handler
        from fastapi.responses import Response as _Resp

        return _Resp(
            content=payload["body"],
            media_type=payload["content_type"],
            status_code=payload.get("http_status", 200),
        )
    return payload


# --- documentation -------------------------------------------------------


@router.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request, user: str = Depends(require_user)):
    return templates.TemplateResponse("docs.html", {"request": request, "user": user})


# --- API tokens ---------------------------------------------------------


def _usage_curl_cmd(request: Request) -> str:
    return (
        f'curl -X POST {request.base_url}api/v1/jobs/etl_daily/runs '
        f'-H "Authorization: Bearer jr_..." '
        f'-H "Content-Type: application/json" '
        f"-d '{{\"params\": {{\"date\": \"2026-11-04\"}}}}'"
    )


@router.get("/tokens", response_class=HTMLResponse)
def tokens_page(
    request: Request,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    tokens = db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    return templates.TemplateResponse(
        "tokens.html",
        {"request": request, "user": user, "tokens": tokens, "new_token": None,
         "usage_cmd": _usage_curl_cmd(request)},
    )


@router.post("/tokens", response_class=HTMLResponse)
def tokens_create(
    request: Request,
    label: str = Form("default"),
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    _row, plaintext = create_token(db, label.strip() or "default")
    tokens = db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    return templates.TemplateResponse(
        "tokens.html",
        {
            "request": request,
            "user": user,
            "tokens": tokens,
            "new_token": plaintext,
            "usage_cmd": _usage_curl_cmd(request),
        },
    )


@router.post("/tokens/{token_id}/revoke")
def tokens_revoke(
    token_id: int,
    user: str = Depends(require_user),
    db: Session = Depends(get_db),
):
    revoke_token(db, token_id)
    return _redirect("/tokens")
