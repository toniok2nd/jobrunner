from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables prefixed JOBRUNNER_."""

    model_config = SettingsConfigDict(env_prefix="JOBRUNNER_")

    secret_key: str = "change-me"
    admin_user: str = "admin"
    admin_password: str = "admin"
    jobs_dir: Path = Path("/srv/jobrunner/jobs")
    data_dir: Path = Path("/srv/jobrunner/data")
    redis_url: str = "redis://localhost:6379/0"
    redis_results_url: str = "redis://localhost:6379/1"
    session_max_age_seconds: int = 7 * 24 * 3600
    task_parallelism: int = 4
    # pip repository / proxy defaults (overridable from the Packages UI)
    pip_index_url: str = ""
    pip_proxy: str = ""  # e.g. http://user:pass@proxy.corp:3128
    webhook_timeout_seconds: int = 60  # max wait for a synchronous /web call

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobrunner.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
