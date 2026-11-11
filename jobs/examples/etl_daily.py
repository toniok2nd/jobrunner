"""Daily ETL: extract -> transform -> {load_warehouse, load_marts} -> notify"""
from jobrunner.dsl import endpoint, task
from demo_utils import current_host  # shared library, jobs/lib/demo_utils.py
import time

schedule = "0 2 * * *"              # optional cron (02:00 daily)
description = "Nightly ETL pipeline"
default_params = {"date": "today"}


@task
def extract(ctx):
    print(f"extracting data for {ctx.params['date']} (run {ctx.run_id[:8]})")
    time.sleep(2)
    return {"rows": 1000}


@task(depends_on=[extract])
def transform(ctx, extract):
    rows = extract["rows"]
    ctx.log(f"transforming {rows} rows")
    time.sleep(2)
    return {"rows": rows, "clean": True}


@task(depends_on=[transform])
def load_warehouse(ctx, transform):
    ctx.log(f"loading {transform['rows']} clean rows into warehouse")
    time.sleep(3)


@task(depends_on=[transform])
def load_marts(ctx, transform):
    ctx.log("rebuilding data marts")
    time.sleep(3)


@task(depends_on=[load_warehouse, load_marts])
def notify(ctx):
    ctx.log(f"ETL complete on {current_host()} - notification sent")


@endpoint("ping")
def ping_handler(ctx):
    """Custom API endpoint: POST /api/v1/web/etl_daily/ping"""
    ctx.log(f"ping received with params: {ctx.params}")
    return {"status": "pong", "params": ctx.params}


@endpoint("run-now")
def run_now_handler(ctx):
    """Trigger the DAG from the webhook itself."""
    from jobrunner.db import SessionLocal
    from jobrunner.loader import load_registry
    from jobrunner.services import trigger_run

    with SessionLocal() as session:
        job = load_registry()["etl_daily"].job
        run = trigger_run(session, job, ctx.params, triggered_by="webhook")
    return {"run_id": run.id}
