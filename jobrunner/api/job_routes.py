from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_db, require_token
from ..loader import load_registry
from ..models import JobRun
from ..services import (
    JobValidationError,
    save_job_code,
    trigger_run,
    validate_job_name,
)

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _run_brief(run: JobRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.id,
        "status": run.status,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "duration_seconds": run.duration_seconds,
    }


@router.get("/jobs", dependencies=[Depends(require_token)])
def list_jobs(db: Session = Depends(get_db)):
    registry = load_registry()
    out = []
    for res in registry.values():
        last_run = db.scalar(
            select(JobRun)
            .where(JobRun.job_name == res.name)
            .order_by(JobRun.queued_at.desc())
            .limit(1)
        )
        job = res.job
        out.append(
            {
                "name": res.name,
                "description": job.description if job else None,
                "schedule": job.schedule if job else None,
                "default_params": job.default_params if job else None,
                "task_count": len(job.tasks) if job else 0,
                "load_error": res.error,
                "last_run": _run_brief(last_run),
            }
        )
    return out


@router.get("/jobs/{name}", dependencies=[Depends(require_token)])
def get_job(name: str, db: Session = Depends(get_db)):
    res = load_registry().get(name)
    if res is None:
        raise HTTPException(status_code=404, detail="job not found")
    j: dict = {
        "name": res.name,
        "load_error": res.error,
        "source_path": res.source_path,
        "mtime": res.mtime,
    }
    if res.job:
        j.update(
            description=res.job.description,
            schedule=res.job.schedule,
            default_params=res.job.default_params,
            task_count=len(res.job.tasks),
            tasks=res.job.task_spec(),
            endpoints=res.job.endpoint_spec(),
            layers=res.job.layers,
        )
    return j


class TriggerBody(BaseModel):
    params: dict = {}


@router.post("/jobs/{name}/runs", status_code=202, dependencies=[Depends(require_token)])
def trigger(name: str, body: TriggerBody, db: Session = Depends(get_db)):
    """The trigger endpoint: enqueue a run of job `name`."""
    res = load_registry().get(name)
    if res is None:
        raise HTTPException(status_code=404, detail="job not found")
    if res.job is None:
        raise HTTPException(status_code=400, detail=f"job failed to load: {res.error}")
    run = trigger_run(db, res.job, body.params, triggered_by="api")
    return {"run_id": run.id, "job": name, "status": run.status}


@router.get("/jobs/{name}/runs", dependencies=[Depends(require_token)])
def list_job_runs(
    name: str,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = (
        select(JobRun)
        .where(JobRun.job_name == name)
        .order_by(JobRun.queued_at.desc())
        .limit(min(limit, 200))
        .offset(offset)
    )
    if status:
        stmt = stmt.where(JobRun.status == status)
    runs = db.scalars(stmt).all()
    return [
        {
            "run_id": r.id,
            "job": r.job_name,
            "status": r.status,
            "triggered_by": r.triggered_by,
            "params": r.params,
            "queued_at": r.queued_at.isoformat() if r.queued_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "duration_seconds": r.duration_seconds,
        }
        for r in runs
    ]


@router.get("/jobs/{name}/code", dependencies=[Depends(require_token)])
def get_code(name: str):
    validate_job_name(name)
    res = load_registry().get(name)
    if res is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "filename": f"{name}.py",
        "source": res.source,
        "mtime": res.mtime,
    }


class CodeBody(BaseModel):
    source: str


@router.put("/jobs/{name}/code", dependencies=[Depends(require_token)])
def put_code(name: str, body: CodeBody):
    """Save job code (creates the job file if it doesn't exist)."""
    try:
        path = save_job_code(name, body.source)
    except JobValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Re-load right away so load errors are reported to the caller.
    res = load_registry().get(name)
    return {
        "saved": True,
        "path": str(path),
        "load_error": res.error if res else None,
    }


@router.post("/jobs/reload", dependencies=[Depends(require_token)])
def reload_jobs():
    registry = load_registry()
    return {
        "jobs": sorted(registry),
        "errors": {name: r.error for name, r in registry.items() if r.error},
    }
