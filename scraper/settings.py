"""Environment-driven runtime settings. Site specifics live in config/site.yaml."""
import os
from dataclasses import dataclass
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    base_url: str
    db_path: Path
    config_path: Path
    log_dir: Path
    log_level: str
    user_agent: str
    delay: float
    timeout: float
    retries: int
    concurrency: int
    max_pages_per_run: int

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        return cls(
            base_url=_env_str("GOMUNIME_BASE_URL", "https://gomunime.top").rstrip("/"),
            db_path=Path(_env_str("DB_PATH", str(root / "data" / "gomunime.db"))),
            config_path=Path(_env_str("SITE_CONFIG", str(root / "config" / "site.yaml"))),
            log_dir=Path(_env_str("LOG_DIR", str(root / "logs"))),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            user_agent=_env_str("USER_AGENT", "gomunime-metadata-bot/1.0"),
            delay=_env_float("REQUEST_DELAY", 0.75),
            timeout=_env_float("REQUEST_TIMEOUT", 25.0),
            retries=_env_int("MAX_RETRIES", 3),
            concurrency=max(1, _env_int("CONCURRENCY", 4)),
            max_pages_per_run=_env_int("MAX_PAGES_PER_RUN", 4000),
        )
