from celery import Celery, signals
from celery.schedules import crontab

from .config import get_settings

_settings = get_settings()

celery = Celery(
    "jobrunner",
    broker=_settings.redis_url,
    backend=_settings.redis_results_url,
    include=["jobrunner.executor", "jobrunner.scheduler"],
)

celery.conf.update(
    task_default_queue="jobs",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # The cron scheduler runs as a single minutely tick that scans job files.
    beat_schedule={
        "jobrunner-scheduler-tick": {
            "task": "jobrunner.scheduler_tick",
            "schedule": crontab(minute="*"),
        }
    },
)


@signals.worker_ready.connect
def _install_log_router_main(**_kwargs):
    from .logcapture import install

    install()


@signals.worker_process_init.connect
def _install_log_router_child(**_kwargs):
    """Each prefork child gets its own router install — Celery replaces
    sys.stdout with a LoggingProxy in children, so installing only in the
    main process would not route task prints to per-task log files."""
    from .logcapture import install

    install()
