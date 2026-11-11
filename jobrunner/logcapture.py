"""Per-task log capture.

A process-global stdout/stderr router is installed once per Celery worker
process. Writes are routed by *thread*: task threads get their own log file;
everything else falls back to the current run's log file (or real stdout when
no run is active). This keeps concurrent tasks' prints in the right files.
"""

import sys
import threading
from contextlib import contextmanager
from pathlib import Path


class _LogRouter:
    def __init__(self, original):
        self._original = original
        self._local = threading.local()
        self._run_fh = None
        self._run_lock = threading.Lock()

    # task-thread routing -------------------------------------------------
    def set_task_fh(self, fh):
        self._local.fh = fh

    def clear_task_fh(self):
        self._local.fh = None

    # run-level fallback --------------------------------------------------
    def set_run_fh(self, fh):
        with self._run_lock:
            self._run_fh = fh

    # file-object protocol -------------------------------------------------
    def _target(self):
        fh = getattr(self._local, "fh", None)
        if fh is not None:
            return fh
        if self._run_fh is not None:
            return self._run_fh
        return self._original

    def write(self, data):
        try:
            self._target().write(data)
        except Exception:  # never let logging kill the task
            pass

    def flush(self):
        try:
            self._target().flush()
        except Exception:
            pass


_stdout_router = _LogRouter(sys.stdout)
_stderr_router = _LogRouter(sys.stderr)
_installed = False


def install() -> None:
    """Route sys.stdout / sys.stderr through the per-thread routers."""
    global _installed
    if _installed:
        return
    sys.stdout = _stdout_router
    sys.stderr = _stderr_router
    _installed = True


def run_log_dir(data_dir: Path, run_id: str) -> Path:
    log_dir = Path(data_dir) / "logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def set_run_log(fh) -> None:
    _stdout_router.set_run_fh(fh)
    _stderr_router.set_run_fh(fh)


def clear_run_log() -> None:
    _stdout_router.set_run_fh(None)
    _stderr_router.set_run_fh(None)


@contextmanager
def task_log(log_dir: Path, task_name: str):
    """Route this thread's stdout/stderr (and ctx.log) to <task_name>.log."""
    path = log_dir / f"{task_name}.log"
    with open(path, "a", encoding="utf-8", buffering=1) as fh:  # line-buffered
        _stdout_router.set_task_fh(fh)
        _stderr_router.set_task_fh(fh)
        try:
            yield fh, path
        finally:
            _stdout_router.clear_task_fh()
            _stderr_router.clear_task_fh()


def log_path(data_dir: Path, run_id: str, task_name: str | None) -> Path | None:
    log_dir = Path(data_dir) / "logs" / run_id
    if not log_dir.is_dir():
        return None
    if task_name:
        # prevent path traversal via the task query param
        safe = Path(task_name).name
        path = log_dir / f"{safe}.log"
    else:
        path = log_dir / "_run.log"
    return path if path.exists() else None
