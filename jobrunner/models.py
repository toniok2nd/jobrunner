import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Run / task statuses
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UPSTREAM_FAILED = "upstream_failed"  # task-level only

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED)


def utcnow() -> datetime:
    """Naive UTC datetime (stored consistently naive in SQLite)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(128))
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow)
    revoked: Mapped[bool] = mapped_column(sa.Boolean, default=False)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(
        sa.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_name: Mapped[str] = mapped_column(sa.String(255), index=True)
    status: Mapped[str] = mapped_column(sa.String(32), default=STATUS_QUEUED, index=True)
    params: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    triggered_by: Mapped[str] = mapped_column(sa.String(32), default="api")  # api|ui|schedule
    celery_task_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)

    tasks: Mapped[list["TaskRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="TaskRun.id"
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return round((end - self.started_at).total_seconds(), 2)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("job_runs.id"), index=True)
    task_name: Mapped[str] = mapped_column(sa.String(255))
    status: Mapped[str] = mapped_column(sa.String(32), default=STATUS_QUEUED, index=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    run: Mapped[JobRun] = relationship(back_populates="tasks")

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return round((end - self.started_at).total_seconds(), 2)


class MetaKV(Base):
    """Small key/value table (e.g. scheduler last-tick watermark)."""

    __tablename__ = "meta_kv"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[str] = mapped_column(sa.Text)
