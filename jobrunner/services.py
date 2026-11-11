"""Shared business logic used by both the REST API and the UI routes."""

import ast
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .celery_app import celery
from .config import get_settings
from .dsl import JobDef
from .models import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    ApiToken,
    JobRun,
    TaskRun,
    utcnow,
)
from .security import generate_token, hash_token

JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class JobValidationError(ValueError):
    pass


# --- runs ---------------------------------------------------------------


def trigger_run(
    session: Session, job: JobDef, params: dict, triggered_by: str = "api"
) -> JobRun:
    merged = dict(job.default_params or {})
    merged.update(params or {})
    run = JobRun(job_name=job.name, params=merged, triggered_by=triggered_by)
    session.add(run)
    session.commit()
    async_result = celery.send_task("jobrunner.run_job", args=[run.id])
    run.celery_task_id = async_result.id
    session.commit()
    return run


def cancel_run(session: Session, run: JobRun) -> bool:
    """Request cancellation. Returns True if the run was cancelled."""
    if run.status not in ACTIVE_STATUSES:
        return False
    if run.celery_task_id:
        celery.control.revoke(run.celery_task_id, terminate=True)
    run.status = STATUS_CANCELLED
    run.finished_at = utcnow()
    # The killed worker child may never update its TaskRun rows; mark any
    # still-active tasks as cancelled so the UI/API stay consistent.
    for t in session.scalars(
        select(TaskRun).where(
            TaskRun.run_id == run.id, TaskRun.status.in_(ACTIVE_STATUSES)
        )
    ).all():
        t.status = STATUS_CANCELLED
        t.finished_at = utcnow()
    session.commit()
    return True


# --- job code -----------------------------------------------------------


def validate_job_name(name: str) -> None:
    if not JOB_NAME_RE.match(name):
        raise JobValidationError(
            "invalid job name: use letters, digits and underscores only"
        )


def validate_python_source(source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise JobValidationError(
            f"SyntaxError at line {exc.lineno}: {exc.msg}"
        ) from exc


def save_job_code(job_name: str, source: str) -> Path:
    validate_job_name(job_name)
    validate_python_source(source)
    path = Path(get_settings().jobs_dir) / f"{job_name}.py"
    path.write_text(source, encoding="utf-8")
    return path


# --- deletion -------------------------------------------------------------


def delete_job_file(job_name: str) -> None:
    validate_job_name(job_name)
    path = Path(get_settings().jobs_dir) / f"{job_name}.py"
    if not path.is_file():
        raise JobValidationError(f"job '{job_name}' not found")
    path.unlink()


def delete_shared_lib(name: str) -> None:
    validate_job_name(name)
    path = Path(get_settings().jobs_dir) / "lib" / f"{name}.py"
    if not path.is_file():
        raise JobValidationError(f"module '{name}' not found")
    path.unlink()


def _endpoint_decorator_path(dec: ast.expr) -> str | None:
    """If `dec` is an @endpoint("path", ...) decorator, return the path."""
    call = dec if isinstance(dec, ast.Call) else None
    func = call.func if call else dec
    is_endpoint = (
        (isinstance(func, ast.Name) and func.id == "endpoint")
        or (isinstance(func, ast.Attribute) and func.attr == "endpoint")
    )
    if not is_endpoint or call is None or not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def delete_endpoint_from_file(job_name: str, endpoint_path: str) -> None:
    """Remove the @endpoint(path) function from the job file by line-span
    splicing — AST-guided, but preserves comments/formatting elsewhere.
    Rolls back if the result fails to parse or load."""
    validate_job_name(job_name)
    file_path = Path(get_settings().jobs_dir) / f"{job_name}.py"
    if not file_path.is_file():
        raise JobValidationError(f"job '{job_name}' not found")
    source = file_path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)

    tree = ast.parse(source)
    span = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if _endpoint_decorator_path(dec) == endpoint_path:
                    start = min(d.lineno for d in node.decorator_list)
                    span = (start, node.end_lineno)
                    break
        if span:
            break
    if span is None:
        raise JobValidationError(
            f"no @endpoint(\"{endpoint_path}\") found in '{job_name}.py'"
        )

    start, end = span  # 1-based, inclusive
    new_lines = lines[: start - 1] + lines[end:]
    new_source = _collapse_blank_runs(new_lines)
    validate_python_source(new_source)

    content_bytes = new_source.encode("utf-8")
    tmp = file_path.with_suffix(".py.tmp")
    tmp.write_bytes(content_bytes)
    tmp.replace(file_path)

    from .loader import load_job_file

    result = load_job_file(file_path)
    if result.error:
        file_path.write_text(source, encoding="utf-8")  # rollback
        raise JobValidationError(f"deletion would break the file: {result.error}")


def _collapse_blank_runs(lines: list[str]) -> str:
    out, blank = [], 0
    for line in lines:
        if line.strip():
            blank = 0
        else:
            blank += 1
            if blank > 2:
                continue
        out.append(line)
    return "".join(out)


# --- custom endpoints (webhooks) -----------------------------------------


class WebhookError(Exception):
    """code: job_not_found | endpoint_not_found | handler_error | timeout"""

    def __init__(self, code: str, payload: dict):
        super().__init__(payload.get("error", code))
        self.code = code
        self.payload = payload


def dispatch_webhook(job_name: str, endpoint_path: str, params: dict,
                     timeout: float | None = None, meta: dict | None = None) -> dict:
    """Run a job's @endpoint handler in the worker, synchronously.

    Records every call as a JobRun (visible in run history with logs).
    Returns {"status": "success", "result", "log", "run_id"}; raises WebhookError.
    """
    from .db import SessionLocal
    from .loader import load_registry

    try:
        validate_job_name(job_name)
    except JobValidationError:
        raise WebhookError("job_not_found", {"error": "invalid job name"})

    result = load_registry().get(job_name)
    if result is None or result.job is None:
        raise WebhookError(
            "job_not_found", {"error": f"job '{job_name}' not found or failed to load"}
        )
    if endpoint_path not in result.job.endpoints:
        raise WebhookError(
            "endpoint_not_found",
            {
                "error": f"job '{job_name}' has no endpoint '{endpoint_path}'",
                "available": sorted(result.job.endpoints),
            },
        )

    merged = dict(result.job.default_params or {})
    merged.update(params or {})
    with SessionLocal() as session:
        run = JobRun(
            job_name=job_name,
            params=merged,
            triggered_by=f"endpoint:{endpoint_path}",
        )
        session.add(run)
        session.commit()
        async_result = celery.send_task(
            "jobrunner.run_webhook",
            args=[job_name, endpoint_path, merged, run.id],
            kwargs={"meta": meta or {}},
        )
        run.celery_task_id = async_result.id
        session.commit()
        run_id = run.id

    timeout = timeout if timeout is not None else get_settings().webhook_timeout_seconds
    from celery.exceptions import TimeoutError as CeleryTimeoutError
    try:
        payload = async_result.get(timeout=timeout)
    except CeleryTimeoutError:
        raise WebhookError(
            "timeout",
            {"status": "timeout",
             "error": f"handler did not answer within {timeout}s "
                      "(still running — it will appear in the run history)",
             "run_id": run_id},
        )
    if payload.get("status") == "failed":
        raise WebhookError(payload.get("code", "handler_error"), payload)
    return payload


# --- API tokens ---------------------------------------------------------


def create_token(session: Session, label: str) -> tuple[ApiToken, str]:
    plaintext = generate_token()
    row = ApiToken(label=label, token_hash=hash_token(plaintext))
    session.add(row)
    session.commit()
    return row, plaintext


def revoke_token(session: Session, token_id: int) -> bool:
    row = session.get(ApiToken, token_id)
    if row is None or row.revoked:
        return False
    row.revoked = True
    session.commit()
    return True
