from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import logcapture
from ..config import get_settings
from ..deps import get_db, require_token
from ..models import ACTIVE_STATUSES, JobRun
from ..services import cancel_run

router = APIRouter(prefix="/api/v1", tags=["runs"])


def _run_detail(run: JobRun) -> dict:
    return {
        "run_id": run.id,
        "job": run.job_name,
        "status": run.status,
        "error": run.error,
        "params": run.params,
        "triggered_by": run.triggered_by,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration_seconds,
        "tasks": [
            {
                "task": t.task_name,
                "status": t.status,
                "error": t.error,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                "duration_seconds": t.duration_seconds,
            }
            for t in run.tasks
        ],
    }


@router.get("/runs", dependencies=[Depends(require_token)])
def list_runs(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    job: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = (
        select(JobRun)
        .order_by(JobRun.queued_at.desc())
        .limit(min(limit, 200))
        .offset(offset)
    )
    if status:
        stmt = stmt.where(JobRun.status == status)
    if job:
        stmt = stmt.where(JobRun.job_name == job)
    return [_run_brief(r) for r in db.scalars(stmt).all()]


def _run_brief(r: JobRun) -> dict:
    return {
        "run_id": r.id,
        "job": r.job_name,
        "status": r.status,
        "triggered_by": r.triggered_by,
        "queued_at": r.queued_at.isoformat() if r.queued_at else None,
        "duration_seconds": r.duration_seconds,
    }


@router.get("/runs/{run_id}", dependencies=[Depends(require_token)])
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_detail(run)


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
def cancel(run_id: str, db: Session = Depends(get_db)):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if not cancel_run(db, run):
        raise HTTPException(status_code=409, detail=f"run is already {run.status}")
    return {"run_id": run_id, "status": run.status}


@router.get(
    "/runs/{run_id}/logs",
    dependencies=[Depends(require_token)],
    response_class=PlainTextResponse,
)
def get_logs(run_id: str, task: str | None = None, db: Session = Depends(get_db)):
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    path = logcapture.log_path(get_settings().data_dir, run_id, task)
    if path is None:
        return PlainTextResponse(
            "(no logs yet)", status_code=200 if run.status in ACTIVE_STATUSES else 404
        )
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))
