"""Discover and import job modules from the jobs directory.

Modules are exec'd fresh on every load (no caching), so code edits saved in the
UI are picked up by the next listing / run without any restart. Import failures
are captured per-job and surfaced as load errors in the UI/API.
"""

import importlib.util
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .config import get_settings
from .dsl import JobDef, build_job


@dataclass
class JobLoadResult:
    name: str
    job: JobDef | None
    source_path: str
    source: str
    mtime: float
    error: str | None


def _load_module(path: Path, unique_name: str) -> ModuleType:
    # pick up packages installed from the Packages UI without a restart,
    # and hot-reload any shared lib module edited in the GUI
    importlib.invalidate_caches()
    lib_dir = Path(get_settings().jobs_dir) / "lib"
    for mod_name, mod in list(sys.modules.items()):
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            try:
                in_lib = Path(mod_file).resolve().is_relative_to(lib_dir.resolve())
            except OSError:
                in_lib = False
            if in_lib:
                try:
                    importlib.reload(mod)
                except Exception:  # a broken lib surfaces via the job's load error
                    pass
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(unique_name, None)
    return module


def load_job_file(path: Path) -> JobLoadResult:
    name = path.stem
    source = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime
    try:
        module = _load_module(path, f"_jobrunner_job_{name}_{time.time_ns()}")
        job = build_job(module, name, str(path), mtime)
        return JobLoadResult(name, job, str(path), source, mtime, None)
    except Exception as exc:  # surface ANY job import error in the UI
        return JobLoadResult(
            name, None, str(path), source, mtime, f"{type(exc).__name__}: {exc}"
        )


def load_registry(jobs_dir: Path | None = None) -> dict[str, JobLoadResult]:
    """Scan the jobs dir and load every *.py file (files starting _ are skipped)."""
    jobs_dir = Path(jobs_dir or get_settings().jobs_dir)
    registry: dict[str, JobLoadResult] = {}
    if not jobs_dir.is_dir():
        return registry
    for path in sorted(jobs_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        result = load_job_file(path)
        registry[result.name] = result
    return registry
