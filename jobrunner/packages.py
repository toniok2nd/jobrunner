"""Install / uninstall extra Python packages from the Packages UI.

Packages are installed with `pip install --target <data_dir>/pythonlibs`.
That directory joins sys.path in every process via jobrunner/__init__.py, so
jobs can import the packages immediately (no restart needed) and they persist
in the data volume across container rebuilds.

Uninstall is file-based: the pip dist-info RECORD tells us exactly which files
a package put down.
"""

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .config import get_settings

# PEP 508-ish: name[extra,extra]==1.2 / >=1.0 ... — no options, no URLs, no spaces
_PKG_SPEC_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(\[[A-Za-z0-9,._]*\])?"
    r"((==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!]+)*$"
)


class PackageSpecError(ValueError):
    pass


# --- pip repository / proxy configuration ----------------------------------
# Priority: GUI override (stored in MetaKV) > JOBRUNNER_* env var > PyPI default.

_KV_INDEX_URL = "pip.index_url"
_KV_PROXY = "pip.proxy"
_URL_PREFIXES = ("http://", "https://")


def _validate_url_or_empty(value: str, what: str) -> str:
    value = value.strip()
    if value and not value.startswith(_URL_PREFIXES):
        raise PackageSpecError(f"invalid {what}: must start with http:// or https://")
    return value


def _kv_get_all() -> dict:
    from .db import SessionLocal
    from .models import MetaKV

    try:
        with SessionLocal() as session:
            return {row.key: row.value for row in session.query(MetaKV).all()}
    except Exception:  # table may not exist yet (first boot)
        return {}


def set_pip_config(index_url: str, proxy: str) -> None:
    """Store GUI overrides; empty string clears the override (env fallback)."""
    from .db import SessionLocal
    from .models import MetaKV

    index_url = _validate_url_or_empty(index_url, "pip index URL")
    proxy = _validate_url_or_empty(proxy, "proxy URL")
    with SessionLocal() as session:
        for key, value in ((_KV_INDEX_URL, index_url), (_KV_PROXY, proxy)):
            row = session.get(MetaKV, key)
            if value:
                if row is None:
                    session.add(MetaKV(key=key, value=value))
                else:
                    row.value = value
            elif row is not None:
                session.delete(row)
        session.commit()


def get_pip_config() -> dict:
    """Effective pip configuration + where each value comes from."""
    kv = _kv_get_all()
    settings = get_settings()

    gui_index, gui_proxy = kv.get(_KV_INDEX_URL), kv.get(_KV_PROXY)
    env_index = settings.pip_index_url.strip()
    env_proxy = settings.pip_proxy.strip()

    return {
        "gui_index_url": gui_index or "",
        "gui_proxy": gui_proxy or "",
        "index_url": gui_index or env_index or "",
        "index_url_source": "gui" if gui_index else ("env" if env_index else "default"),
        "proxy": gui_proxy or env_proxy or "",
        "proxy_source": "gui" if gui_proxy else ("env" if env_proxy else "default"),
    }


def mask_credentials(url: str) -> str:
    """http://user:secret@host -> http://user:****@host"""
    return re.sub(r"(://[^/:@\s]+:)[^@\s]+@", r"\1****@", url)


def extra_lib_dir() -> Path:
    d = Path(get_settings().data_dir) / "pythonlibs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_spec(spec: str) -> str:
    spec = spec.strip()
    if not spec or spec.startswith("-") or not _PKG_SPEC_RE.match(spec):
        raise PackageSpecError(
            f"invalid package specifier '{spec!r}' — expected e.g. 'requests', "
            "'pandas>=2.0' or 'apache-airflow-client[async]==2.7'"
        )
    return spec


# --- listings -------------------------------------------------------------


def list_extra_packages() -> list[dict]:
    out = []
    for di in sorted(extra_lib_dir().glob("*.dist-info")):
        name, _, version = di.name[: -len(".dist-info")].rpartition("-")
        out.append({"name": name.replace("_", "-"), "version": version,
                    "dist_info": di.name})
    return out


def list_base_packages() -> list[dict]:
    return sorted(
        (
            {"name": d.metadata.get("Name", "?"), "version": d.version}
            for d in importlib.metadata.distributions()
        ),
        key=lambda p: p["name"].lower(),
    )


# --- install operations (background, one pip at a time) --------------------

_lock = threading.Lock()
_ops: dict[str, dict] = {}


def start_install(spec: str) -> str:
    spec = validate_spec(spec)
    with _lock:
        running = [o for o in _ops.values() if o["status"] == "running"]
        if running:
            raise PackageSpecError(
                f"an install is already running: {running[0]['spec']}"
            )
        op_id = uuid.uuid4().hex[:12]
        _ops[op_id] = {"spec": spec, "status": "running", "lines": []}
    thread = threading.Thread(target=_run_install, args=(op_id, spec), daemon=True)
    thread.start()
    return op_id


def _run_install(op_id: str, spec: str) -> None:
    op = _ops[op_id]
    cfg = get_pip_config()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        "--target", str(extra_lib_dir()),
    ]
    if cfg["index_url"]:
        cmd += ["--index-url", cfg["index_url"]]
    cmd.append(spec)

    env = os.environ.copy()
    if cfg["proxy"]:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[var] = cfg["proxy"]

    masked = " ".join(mask_credentials(part) for part in cmd)
    op["lines"].append("$ " + masked)
    if cfg["proxy"]:
        op["lines"].append(
            f"(proxy: {mask_credentials(cfg['proxy'])}, from {cfg['proxy_source']})"
        )
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        for line in proc.stdout:
            op["lines"].append(line.rstrip())
        rc = proc.wait()
        op["status"] = "success" if rc == 0 else "failed"
        op["lines"].append(f"(exit code {rc})")
    except Exception as exc:
        op["status"] = "failed"
        op["lines"].append(f"{type(exc).__name__}: {exc}")
    finally:
        importlib.invalidate_caches()


def get_op(op_id: str) -> dict | None:
    return _ops.get(op_id)


# --- uninstall ------------------------------------------------------------


def uninstall(dist_info_name: str) -> str:
    """Remove a package installed with --target, using its RECORD manifest.

    Takes the dist-info directory name (e.g. 'requests-2.31.0.dist-info')
    so there is no ambiguity about which files to delete.
    """
    root = extra_lib_dir().resolve()
    di = (root / dist_info_name).resolve()
    if (
        di.parent != root
        or not di.name.endswith(".dist-info")
        or not di.is_dir()
    ):
        raise PackageSpecError("invalid package directory")

    record = di / "RECORD"
    if record.is_file():
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            rel = line.split(",", 1)[0].strip()
            if not rel:
                continue
            target = (root / rel).resolve()
            if root in target.parents and target.is_file():
                target.unlink()
    shutil.rmtree(di)

    # drop now-empty directories, deepest first
    for d in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(d.iterdir()):
            d.rmdir()

    importlib.invalidate_caches()
    return dist_info_name[: -len(".dist-info")]
