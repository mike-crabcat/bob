"""Configuration helpers for the Cyborg service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_POOL_SIZE = 4


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default.expanduser()


@dataclass(slots=True)
class Settings:
    """Runtime settings for the API service and CLI."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path("~/.local/share/cyborg")
    config_dir: Path = Path("~/.config/cyborg")
    db_path: Path | None = None
    log_level: str = "info"
    pool_size: int = DEFAULT_POOL_SIZE

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser()
        self.config_dir = self.config_dir.expanduser()
        if self.db_path is None:
            self.db_path = self.data_dir / "cyborg.db"
        else:
            self.db_path = self.db_path.expanduser()

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""

        data_dir = _env_path("CYBORG_DATA_DIR", Path("~/.local/share/cyborg"))
        config_dir = _env_path("CYBORG_CONFIG_DIR", Path("~/.config/cyborg"))
        db_path_value = os.getenv("CYBORG_DB_PATH")
        db_path = Path(db_path_value).expanduser() if db_path_value else data_dir / "cyborg.db"
        host = os.getenv("CYBORG_HOST", DEFAULT_HOST)
        port = int(os.getenv("CYBORG_PORT", str(DEFAULT_PORT)))
        pool_size = int(os.getenv("CYBORG_DB_POOL_SIZE", str(DEFAULT_POOL_SIZE)))
        log_level = os.getenv("CYBORG_LOG_LEVEL", "info")
        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=db_path,
            log_level=log_level,
            pool_size=pool_size,
        )

    def ensure_directories(self) -> None:
        """Create the configured data and config directories."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
