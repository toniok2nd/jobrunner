"""Micro-DSL for defining jobs.

A job is one Python file in the jobs directory. Task functions are decorated
with @task and may declare dependencies:

    from jobrunner.dsl import task

    @task
    def extract(ctx):
        return {"rows": 10}

    @task(depends_on=[extract])
    def transform(ctx, extract):     # upstream result injected by param name
        ...
"""

import inspect
import re
from dataclasses import dataclass, field
from types import ModuleType
from typing import Callable


@dataclass
class TaskDef:
    name: str
    fn: Callable
    depends_on: list[str] = field(default_factory=list)


_ENDPOINT_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass
class EndpointDef:
    """A custom HTTP endpoint attached to a job:
    POST/GET /api/v1/web/<job>/<path> executes fn in the worker."""

    path: str
    fn: Callable

    @property
    def name(self) -> str:
        return self.fn.__name__


def endpoint(path: str):
    """@endpoint("deploy") -> the decorated function is executed when
    /api/v1/web/<job>/deploy is called. It gets ctx (params = request JSON
    body) and its return value becomes the JSON response."""

    def wrap(func: Callable) -> EndpointDef:
        if not _ENDPOINT_PATH_RE.match(path):
            raise ValueError(
                f"invalid endpoint path {path!r}: letters, digits, - and _ only"
            )
        return EndpointDef(path=path, fn=func)

    return wrap


class EndpointResponse:
    """Custom HTTP response an @endpoint handler can return instead of JSON.

    Usage:
        from jobrunner.dsl import endpoint, html, text

        @endpoint("report")
        def report(ctx):
            return html("<h1>Report</h1>")
    """

    def __init__(
        self,
        body: str,
        content_type: str = "text/plain",
        status: int = 200,
        headers: dict | None = None,
    ):
        self.body = body if isinstance(body, str) else str(body)
        self.content_type = content_type
        self.status = status
        self.headers = dict(headers or {})


def html(body: str, status: int = 200, headers: dict | None = None) -> EndpointResponse:
    """Return a full HTML page from an endpoint handler."""
    return EndpointResponse(body, "text/html; charset=utf-8", status, headers)


def text(body: str, status: int = 200, headers: dict | None = None) -> EndpointResponse:
    """Return plain text from an endpoint handler."""
    return EndpointResponse(body, "text/plain; charset=utf-8", status, headers)


def _dep_name(dep) -> str:
    if isinstance(dep, TaskDef):
        return dep.name
    if callable(dep):
        return dep.__name__
    if isinstance(dep, str):
        return dep
    raise TypeError(
        f"Unsupported dependency {dep!r}: pass a task function or a task name"
    )


def task(fn=None, *, depends_on=None):
    """Decorate a function as a job task. Usable bare (@task) or with args."""

    def wrap(func: Callable) -> TaskDef:
        deps = [_dep_name(d) for d in (depends_on or [])]
        return TaskDef(name=func.__name__, fn=func, depends_on=deps)

    if fn is not None:  # bare @task
        return wrap(fn)
    return wrap


@dataclass
class JobDef:
    name: str
    description: str
    schedule: str | None
    default_params: dict
    tasks: dict[str, TaskDef]
    endpoints: dict[str, EndpointDef]
    layers: list[list[str]]  # topological layers of simultaneously-runnable tasks
    source_path: str
    mtime: float

    def task_spec(self) -> list[dict]:
        return [
            {"task": t.name, "depends_on": t.depends_on} for t in self.tasks.values()
        ]

    def endpoint_spec(self) -> list[dict]:
        return [
            {"path": e.path, "handler": e.name} for e in self.endpoints.values()
        ]


def build_job(module: ModuleType, name: str, source_path: str, mtime: float) -> JobDef:
    tasks: dict[str, TaskDef] = {}
    endpoints: dict[str, EndpointDef] = {}
    for attr in vars(module).values():
        if isinstance(attr, TaskDef):
            tasks[attr.name] = attr
        elif isinstance(attr, EndpointDef):
            endpoints[attr.path] = attr
    if not tasks and not endpoints:
        raise ValueError("job module defines neither @task functions nor @endpoint handlers")

    for t in tasks.values():
        for dep in t.depends_on:
            if dep not in tasks:
                raise ValueError(
                    f"task '{t.name}' depends on unknown task '{dep}'"
                )
        params = list(inspect.signature(t.fn).parameters)
        body_params = params[1:] if params and params[0] == "ctx" else params
        for p in body_params:
            if p not in t.depends_on:
                raise ValueError(
                    f"task '{t.name}' takes parameter '{p}' but does not declare "
                    f"it in depends_on={t.depends_on}"
                )

    return JobDef(
        name=name,
        description=(getattr(module, "description", "") or "").strip(),
        schedule=(getattr(module, "schedule", None) or None),
        default_params=dict(getattr(module, "default_params", {}) or {}),
        tasks=tasks,
        endpoints=endpoints,
        layers=topological_layers(tasks),
        source_path=source_path,
        mtime=mtime,
    )


def topological_layers(tasks: dict[str, TaskDef]) -> list[list[str]]:
    """Kahn's algorithm, grouped into layers of tasks that can run in parallel."""
    remaining = {name: set(t.depends_on) for name, t in tasks.items()}
    done: set[str] = set()
    layers: list[list[str]] = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if deps <= done)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(f"dependency cycle detected among tasks: {cycle}")
        layers.append(ready)
        done.update(ready)
        for n in ready:
            remaining.pop(n)
    return layers
