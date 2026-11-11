# jobrunner

An Airflow-lite Python job runner in a **single self-contained Docker image**
(web server + Redis + workers + scheduler, all inside). Jobs are DAGs of Python
functions defined in plain `.py` files, triggerable via REST API, cron, or the
built-in authenticated web UI — which also lets you monitor runs and **edit job
code in the browser**.

## Quick start

```bash
docker build -t jobrunner .

docker run -d --name jobrunner -p 8080:8080 \
  -e JOBRUNNER_ADMIN_PASSWORD=secret \
  -e JOBRUNNER_SECRET_KEY=$(openssl rand -hex 32) \
  -v jobrunner-jobs:/srv/jobrunner/jobs \
  -v jobrunner-data:/srv/jobrunner/data \
  jobrunner
```

Open http://localhost:8080 and sign in with `admin` / your password. Two example
jobs are seeded into the jobs volume on first boot (`etl_daily`, `heartbeat`).

## Triggering a job via API

Create a token in the UI (**API tokens** page), then:

```bash
curl -X POST http://localhost:8080/api/v1/jobs/etl_daily/runs \
  -H "Authorization: Bearer jr_..." \
  -H "Content-Type: application/json" \
  -d '{"params": {"date": "2026-11-04"}}'
# → {"run_id": "...", "status": "queued"}

curl http://localhost:8080/api/v1/runs/<run_id> \
  -H "Authorization: Bearer jr_..."
```

Interactive API docs are at `/docs` (OpenAPI). Main endpoints:

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/health` | Liveness + Redis check (unauthenticated) |
| GET | `/api/v1/jobs` | List jobs |
| GET | `/api/v1/jobs/{name}` | Job metadata + task graph |
| POST | `/api/v1/jobs/{name}/runs` | **Trigger a run** (`{"params": {...}}`) |
| GET | `/api/v1/jobs/{name}/runs` | Runs of a job |
| GET | `/api/v1/runs/{run_id}` | Run detail incl. per-task status |
| POST | `/api/v1/runs/{run_id}/cancel` | Cancel a queued/running run |
| GET | `/api/v1/runs/{run_id}/logs?task=<t>` | Plain-text logs |
| GET/PUT | `/api/v1/jobs/{name}/code` | Read/save job source (validated) |
| POST | `/api/v1/jobs/reload` | Re-import job files |

## Custom endpoints (`@endpoint`)

A job file can declare its own HTTP endpoints — synchronous Python executed in
the worker, with the handler's return value as the JSON response:

```python
from jobrunner.dsl import task, endpoint

@endpoint("deploy")                # POST /api/v1/web/etl_daily/deploy
def deploy_handler(ctx):
    ctx.log(f"deploy params: {ctx.params}")      # request JSON body
    return {"deployed": True}                    # -> JSON response
```

```bash
curl -X POST http://localhost:8080/api/v1/web/etl_daily/deploy \
  -H "Authorization: Bearer jr_..." -H "Content-Type: application/json" \
  -d '{"env": "prod"}'
# → {"status": "success", "result": {"deployed": true}, "log": "…"}
```

- Return HTML pages / plain text instead of JSON with `html()` / `text()`:

```python
from jobrunner.dsl import endpoint, html

@endpoint("report")
def report(ctx):
    return html(f"<h1>Report for run {ctx.run_id[:8]}</h1>", status=200)
```

- Same bearer-token auth as the rest of the API; `GET` also works (query params).
- Responses: `200` + handler result · `404` unknown job/endpoint ·
  `500` handler raised (with traceback tail in `log`) · `504` if the handler
  exceeds `JOBRUNNER_WEBHOOK_TIMEOUT_SECONDS` (default 60) — it keeps running.
- A handler can trigger the job's own DAG via `jobrunner.services.trigger_run`
  (see the `run-now` endpoint in the example job).
- The **Endpoints** page in the UI lists every endpoint, lets you **create**
  one (new file or append to an existing job file — skeleton generated, then
  edit in the built-in editor) and **test** it directly from the browser.
  `GET /api/v1/webhooks` lists them via the API.

## Defining jobs

One file in `jobs/` = one job (job name = filename stem). Tasks are functions
decorated with `@task`; dependencies are declared with `depends_on` and upstream
return values are injected by parameter name:

```python
from jobrunner.dsl import task

schedule = "0 2 * * *"                 # optional cron
description = "Nightly ETL"
default_params = {"date": "today"}     # merged with trigger params

@task
def extract(ctx):
    ctx.log(f"run {ctx.run_id}, params: {ctx.params}")
    return {"rows": 1000}

@task(depends_on=[extract])
def transform(ctx, extract):           # extract's return value injected here
    ...

@task(depends_on=[transform])
def load_a(ctx, transform): ...        # independent tasks in the same

@task(depends_on=[transform])
def load_b(ctx, transform): ...        # layer run concurrently

@task(depends_on=[load_a, load_b])
def notify(ctx): ...
```

Task prints and `ctx.log(...)` lines are captured per task into
`data/logs/<run_id>/<task>.log` and viewable live in the UI.

## Installing extra Python packages

The **Packages** page in the UI installs libraries into the job environment:

- Packages are installed with `pip install --target <data_dir>/pythonlibs/`
  (inside the **data volume** — they survive `docker rm` / rebuilds).
- The directory joins `sys.path` automatically in the web server **and** the
  workers, so jobs can `import` them immediately — **no restart needed**.
- Use normal pip specs: `pandas>=2.0`, `requests[socks]`, `ansicolors==1.1.8`.
- Uninstall removes exactly the files recorded in the package's pip manifest.

Example job after installing `ansicolors` from the UI:

```python
from jobrunner.dsl import task
import colors

@task
def demo(ctx):
    ctx.log(colors.red("installed via the Packages page"))
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `JOBRUNNER_ADMIN_USER` | `admin` | Admin username (seeded on first boot only) |
| `JOBRUNNER_ADMIN_PASSWORD` | `admin` | Admin password (seeded on first boot only) |
| `JOBRUNNER_SECRET_KEY` | `change-me` | Session-cookie signing key — **change it** |
| `JOBRUNNER_JOBS_DIR` | `/srv/jobrunner/jobs` | Where job `.py` files live |
| `JOBRUNNER_DATA_DIR` | `/srv/jobrunner/data` | SQLite DB, logs, scheduler state |
| `JOBRUNNER_REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `JOBRUNNER_REDIS_RESULTS_URL` | `redis://localhost:6379/1` | Celery result backend |
| `JOBRUNNER_ENABLE_BEAT` | `true` | Set to `false` to disable cron scheduling |
| `JOBRUNNER_TASK_PARALLELISM` | `4` | Max concurrent tasks within one run |
| `JOBRUNNER_PIP_INDEX_URL` | *(PyPI)* | Custom pip index, e.g. `https://pypi.mycorp.local/simple` |
| `JOBRUNNER_PIP_PROXY` | *(none)* | Proxy for package installs, auth supported: `http://user:pass@proxy:3128` |

Both pip settings can also be overridden from the **Packages** page in the UI
(GUI values take precedence over the environment variables; credentials are
masked in logs and in the UI).

## Security notice

Jobs are **trusted code**: anything saved in the browser editor runs with full
container privileges. The container is the trust boundary — put it behind auth
and never expose it to untrusted networks. Both the UI (session login, argon2
password hashing) and the API (bearer tokens, hashed at rest) are authenticated.

## Architecture

```
supervisord
 ├── redis-server          queue broker + result backend
 ├── uvicorn (FastAPI)     REST API + server-rendered UI
 ├── celery worker         executes runs (DAG engine in jobrunner/executor.py)
 └── celery beat           minutely cron tick (optional)
Volumes: jobs/ (job code) · data/ (jobrunner.db, logs/)
```

Runs are executed as one Celery task per run; the DAG engine
(`jobrunner/dsl.py`, `jobrunner/executor.py`) topologically sorts tasks and runs
each dependency layer on a thread pool, capturing per-task logs via a
thread-routed stdout proxy (`jobrunner/logcapture.py`).

## Local development (without Docker)

```bash
pip install -r requirements.txt
redis-server --daemonize yes
JOBRUNNER_DATA_DIR=$PWD/data JOBRUNNER_JOBS_DIR=$PWD/jobs PYTHONPATH=$PWD \
  uvicorn jobrunner.main:app --port 8080 &
JOBRUNNER_DATA_DIR=$PWD/data JOBRUNNER_JOBS_DIR=$PWD/jobs PYTHONPATH=$PWD \
  celery -A jobrunner.celery_app:celery worker --concurrency=2 -Q jobs &
```
