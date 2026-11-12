from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from .config import get_settings


def make_engine():
    settings = get_settings()
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables and seed the admin user. Idempotent."""
    import logging

    from .models import Base, User
    from .security import hash_password

    log = logging.getLogger("jobrunner")
    settings = get_settings()
    if settings.secret_key == "change-me":
        log.warning(
            "INSECURE: JOBRUNNER_SECRET_KEY is the built-in default. "
            "Admin sessions are forgeable by anyone. Set the env var, or run "
            "via the Docker entrypoint which generates a random one."
        )
    if settings.admin_password == "admin":
        log.warning(
            "INSECURE: JOBRUNNER_ADMIN_PASSWORD is the built-in default. "
            "Set the env var, or run via the Docker entrypoint which "
            "generates a random password at first boot."
        )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        exists = session.scalar(
            select(User.id).where(User.username == settings.admin_user)
        )
        if exists is None:
            session.add(
                User(
                    username=settings.admin_user,
                    password_hash=hash_password(settings.admin_password),
                )
            )
            session.commit()
