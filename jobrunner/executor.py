"""Celery task that executes a whole job DAG for one JobRun.

Strategy: one Celery task per run. Inside it, tasks of the DAG are executed
layer by layer; independent tasks within a layer run concurrently on a
ThreadPoolExecutor. Each wave re-checks the run row so cancellations requested
via the API stop the run promptly. All state transitions go through SQLite,
which the web process reads for the UI/API.
"""

import inspect
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select

from . import logcapture
from .celery_app import celery
from .config import get_settings
from .db import SessionLocal
from .dsl import JobDef
from .loader import load_registry
from .models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_UPSTREAM_FAILED,
    JobRun,
    TaskRun,
    utcnow,
)


class TaskContext:
    """Passed as `ctx` to every task function."""

    def __init__(self, run_id: str, params: dict, attempt: int = 1):
        self.run_id = run_id
        self.params = params
        self.attempt = attempt

    def log(self, message) -> None:
        # print() is routed per-thread to this task's own log file
        print(message)


def _build_call(task, ctx, results: dict):
    sig = list(inspect.signature(task.fn).parameters)
    args = [ctx] if sig and sig[0] == "ctx" else []
    dep_params = sig[1:] if args else sig
    kwargs = {p: results.get(p) for p in dep_params}
    return args, kwargs


def _execute_task(task, run_id: str, params: dict, results: dict, log_dir: Path):
    """Run one task in the current thread. Returns (ok, return_value)."""
    with SessionLocal() as session:
        tr = session.scalar(
            select(TaskRun).where(
                TaskRun.run_id == run_id, TaskRun.task_name == task.name
            )
        )
        tr.status = STATUS_RUNNING
        tr.started_at = utcnow()
        session.commit()

    ctx = TaskContext(run_id, params)
    args, kwargs = _build_call(task, ctx, results)
    ok, value, error = True, None, None

    with logcapture.task_log(log_dir, task.name) as (fh, _path):
        try:
            fh.write(f"=== task '{task.name}' started {utcnow().isoformat()}Z ===\n")
            value = task.fn(*args, **kwargs)
            fh.write(f"=== task '{task.name}' finished {utcnow().isoformat()}Z ===\n")
        except Exception:
            ok = False
            error = traceback.format_exc()
            fh.write(f"\n--- TASK FAILED ---\n{error}")

    with SessionLocal() as session:
        tr = session.scalar(
            select(TaskRun).where(
                TaskRun.run_id == run_id, TaskRun.task_name == task.name
            )
        )
        tr.finished_at = utcnow()
        tr.status = STATUS_SUCCESS if ok else STATUS_FAILED
        if error:
            tr.error = error.strip().splitlines()[-1]
        session.commit()

    return ok, value


def _mark_task(run_id: str, task_name: str, status: str) -> None:
    with SessionLocal() as session:
        tr = session.scalar(
            select(TaskRun).where(
                TaskRun.run_id == run_id, TaskRun.task_name == task_name
            )
        )
        if tr:
            tr.status = status
            tr.finished_at = utcnow()
            session.commit()


def _run_cancelled(run_id: str) -> bool:
    with SessionLocal() as session:
        run = session.get(JobRun, run_id)
        return run is not None and run.status == STATUS_CANCELLED


def _execute_dag(job: JobDef, run_id: str, params: dict, log_dir: Path) -> None:
    settings = get_settings()

    with SessionLocal() as session:
        for name in job.tasks:
            session.add(TaskRun(run_id=run_id, task_name=name, status=STATUS_QUEUED))
        session.commit()

    results: dict[str, object] = {}
    status = {name: STATUS_QUEUED for name in job.tasks}
    pending = set(job.tasks)

    while pending:
        if _run_cancelled(run_id):
            for name in pending:
                _mark_task(run_id, name, STATUS_CANCELLED)
            return

        ready = [
            name
            for name in sorted(pending)
            if all(
                status[dep] not in (STATUS_QUEUED, STATUS_RUNNING)
                for dep in job.tasks[name].depends_on
            )
        ]
        if not ready:
            break  # cannot happen for a validated DAG

        runnable = [
            name
            for name in ready
            if all(status[dep] == STATUS_SUCCESS for dep in job.tasks[name].depends_on)
        ]
        for name in ready:
            if name not in runnable:
                status[name] = STATUS_UPSTREAM_FAILED
                pending.discard(name)
                _mark_task(run_id, name, STATUS_UPSTREAM_FAILED)
                print(f"task '{name}' skipped: a dependency failed")

        if not runnable:
            continue

        workers = min(settings.task_parallelism, len(runnable))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _execute_task,
                    job.tasks[name],
                    run_id,
                    params,
                    dict(results),
                    log_dir,
                ): name
                for name in runnable
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    ok, value = future.result()
                except Exception:  # defensive: _execute_task shouldn't raise
                    ok, value = False, None
                    _mark_task(run_id, name, STATUS_FAILED)
                status[name] = STATUS_SUCCESS if ok else STATUS_FAILED
                if ok:
                    results[name] = value
                pending.discard(name)


def _finalize_run(run_id: str) -> None:
    with SessionLocal() as session:
        run = session.get(JobRun, run_id)
        if run is None or run.status == STATUS_CANCELLED:
            return
        task_statuses = [t.status for t in run.tasks]
        if any(
            s in (STATUS_FAILED, STATUS_UPSTREAM_FAILED) for s in task_statuses
        ):
            run.status = STATUS_FAILED
            failed = next(
                (t for t in run.tasks if t.status == STATUS_FAILED), None
            )
            run.error = (
                f"task '{failed.task_name}': {failed.error}" if failed else "failed"
            )
        else:
            run.status = STATUS_SUCCESS
        run.finished_at = utcnow()
        session.commit()


@celery.task(name="jobrunner.run_webhook")
def run_webhook_task(job_name: str, endpoint_path: str, params: dict,
                     run_id: str, meta: dict | None = None) -> dict:
    """Execute a job's custom @endpoint handler synchronously.

    Called via POST/GET /api/v1/web/<job>/<path>. Recorded as a JobRun (visible
    in the run history) with one TaskRun named after the endpoint path, and the
    handler's stdout/ctx.log captured to the run's log directory — retrievable
    later via /api/v1/runs/<run_id>/logs?task=<path> and the UI run page.
    The handler's return value is returned to the HTTP caller.
    """
    logcapture.install()
    settings = get_settings()
    log_dir = logcapture.run_log_dir(settings.data_dir, run_id)
    run_fh = open(log_dir / "_run.log", "a", encoding="utf-8", buffering=1)
    logcapture.set_run_log(run_fh)
    task_log_file = log_dir / f"{endpoint_path}.log"

    def _fail(code: str, error: str) -> dict:
        with SessionLocal() as session:
            run = session.get(JobRun, run_id)
            if run is not None:
                run.status = STATUS_FAILED
                run.error = error
                run.finished_at = utcnow()
            tr = session.scalar(
                select(TaskRun).where(
                    TaskRun.run_id == run_id, TaskRun.task_name == endpoint_path
                )
            )
            if tr is not None:
                tr.status = STATUS_FAILED
                tr.error = error
                tr.finished_at = utcnow()
            session.commit()
        return {"status": "failed", "code": code, "error": error,
                "log": _read_tail(task_log_file), "run_id": run_id}

    try:
        meta = meta or {}
        header_lines = [
            f"=== endpoint {job_name}/{endpoint_path} (run {run_id[:8]}) "
            f"{utcnow().isoformat()}Z ===",
            f"request:    {meta.get('method', '-')} {meta.get('url', '-')}",
            f"params:     {params}",
            f"client:     {meta.get('client', '-')}  "
            f"user-agent: {meta.get('user_agent', '-')}",
        ]
        # auth context = session user or token LABEL only; the bearer token
        # itself is never passed here and never logged.
        if meta.get("auth"):
            header_lines.append(f"auth:       {meta['auth']}")
        request_header = "\n".join(header_lines) + "\n"
        print(request_header, end="")

        with SessionLocal() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                return _fail("job_not_found", f"run '{run_id}' not found")
            if run.status == STATUS_CANCELLED:
                return {"status": "cancelled", "run_id": run_id}
            run.status = STATUS_RUNNING
            run.started_at = utcnow()
            session.add(
                TaskRun(run_id=run_id, task_name=endpoint_path,
                        status=STATUS_RUNNING, started_at=utcnow())
            )
            session.commit()

        result = load_registry().get(job_name)
        if result is None or result.job is None:
            return _fail(
                "job_not_found",
                f"job '{job_name}' not found or failed to load"
                + (f": {result.error}" if result else ""),
            )
        endpoint = result.job.endpoints.get(endpoint_path)
        if endpoint is None:
            return _fail(
                "endpoint_not_found",
                f"job '{job_name}' has no endpoint '{endpoint_path}'",
            )

        ctx = TaskContext(run_id, params or {})
        args = [ctx] if _takes_ctx(endpoint.fn) else []
        ok, value, error = True, None, None
        with logcapture.task_log(log_dir, endpoint_path) as (fh, _p):
            try:
                fh.write(request_header)
                value = endpoint.fn(*args)
                fh.write(f"=== endpoint '{endpoint_path}' finished "
                         f"{utcnow().isoformat()}Z ===\n")
            except Exception:
                ok = False
                error = traceback.format_exc()
                fh.write(f"\n--- HANDLER FAILED ---\n{error}")

        with SessionLocal() as session:
            run = session.get(JobRun, run_id)
            tr = session.scalar(
                select(TaskRun).where(
                    TaskRun.run_id == run_id, TaskRun.task_name == endpoint_path
                )
            )
            tr.status = STATUS_SUCCESS if ok else STATUS_FAILED
            tr.finished_at = utcnow()
            if error:
                tr.error = error.strip().splitlines()[-1]
            if run.status != STATUS_CANCELLED:
                run.status = STATUS_SUCCESS if ok else STATUS_FAILED
                run.error = None if ok else tr.error
                run.finished_at = utcnow()
            session.commit()

        if not ok:
            return {"status": "failed", "code": "handler_error",
                    "error": tr.error, "log": _read_tail(task_log_file),
                    "run_id": run_id}
        print(f"=== done -> {value!r} ===")
        return {"status": "success", "result": value,
                "log": _read_tail(task_log_file), "run_id": run_id}
    finally:
        logcapture.clear_run_log()
        run_fh.close()


def _takes_ctx(fn) -> bool:
    params = list(inspect.signature(fn).parameters)
    return bool(params) and params[0] == "ctx"


def run_id_uuid() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _read_tail(path: Path, max_bytes: int = 16_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


@celery.task(name="jobrunner.run_job", bind=True)
def run_job_task(self, run_id: str) -> None:
    logcapture.install()  # idempotent; ensure routing in this worker child
    settings = get_settings()
    log_dir = logcapture.run_log_dir(settings.data_dir, run_id)
    run_fh = open(log_dir / "_run.log", "a", encoding="utf-8", buffering=1)
    logcapture.set_run_log(run_fh)
    try:
        print(f"=== run {run_id} started {utcnow().isoformat()}Z ===")
        with SessionLocal() as session:
            run = session.get(JobRun, run_id)
            if run is None or run.status == STATUS_CANCELLED:
                return
            run.status = STATUS_RUNNING
            run.started_at = utcnow()
            session.commit()
            job_name = run.job_name
            params = run.params or {}

        result = load_registry().get(job_name)
        if result is None:
            job, load_error = None, "job file not found"
        elif result.job is None:
            job, load_error = None, result.error
        else:
            job, load_error = result.job, None

        if job is None:
            print(f"run aborted: could not load job '{job_name}': {load_error}")
            with SessionLocal() as session:
                run = session.get(JobRun, run_id)
                run.status = STATUS_FAILED
                run.error = f"could not load job '{job_name}': {load_error}"
                run.finished_at = utcnow()
                session.commit()
            return

        _execute_dag(job, run_id, params, log_dir)
        _finalize_run(run_id)
        print(f"=== run {run_id} finished {utcnow().isoformat()}Z ===")
    finally:
        logcapture.clear_run_log()
        run_fh.close()
