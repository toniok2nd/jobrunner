"""Cron scheduling.

Celery beat fires `scheduler_tick` once a minute. The tick scans the job files
for `schedule = "<cron expr>"` attributes and dispatches a run for every cron
occurrence due since the last tick. A watermark in the DB (MetaKV) makes ticks
idempotent and avoids backfill storms on first boot.
"""

from datetime import datetime, timezone

from croniter import croniter

from .celery_app import celery
from .db import SessionLocal
from .loader import load_registry
from .models import MetaKV
from .services import trigger_run

LAST_TICK_KEY = "scheduler_last_tick"


@celery.task(name="jobrunner.scheduler_tick")
def scheduler_tick() -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)

    with SessionLocal() as session:
        row = session.get(MetaKV, LAST_TICK_KEY)
        if row is None:
            # First tick ever: set the watermark to now, don't backfill.
            session.add(MetaKV(key=LAST_TICK_KEY, value=now.isoformat()))
            session.commit()
            return
        last = datetime.fromisoformat(row.value)

    registry = load_registry()
    for res in registry.values():
        job = res.job
        if job is None or not job.schedule:
            continue
        try:
            itr = croniter(job.schedule, last)
            nxt = itr.get_next(datetime)
            while nxt <= now:
                with SessionLocal() as session:
                    trigger_run(session, job, dict(job.default_params),
                                triggered_by="schedule")
                print(f"scheduled run of '{job.name}' (due {nxt.isoformat()})")
                nxt = itr.get_next(datetime)
        except Exception as exc:  # invalid cron expression, etc.
            print(f"scheduler: skipping job '{job.name}': {exc}")

    with SessionLocal() as session:
        row = session.get(MetaKV, LAST_TICK_KEY)
        if row is not None:
            row.value = now.isoformat()
            session.commit()
