"""Configuration helpers for the Cyborg service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse, urlunparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_POOL_SIZE = 4
DEFAULT_ENV_FILE_NAME = ".env"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default.expanduser()


def _load_cyborg_env_files() -> None:
    """Load `.env` files into the process environment without overriding explicit env vars.

    Precedence:
    1. Existing process environment
    2. `CYBORG_ENV_FILE`, if set
    3. `.env` in the current working directory
    4. `.env` in the resolved Cyborg config directory
    """

    candidates: list[Path] = []
    explicit_env_file = os.getenv("CYBORG_ENV_FILE")
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser())

    candidates.append(Path.cwd() / DEFAULT_ENV_FILE_NAME)

    for path in candidates:
        _load_env_file(path)

    config_dir = _env_path("CYBORG_CONFIG_DIR", Path("~/.config/cyborg"))
    _load_env_file(config_dir / DEFAULT_ENV_FILE_NAME)


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from a `.env` file."""

    if not path.exists() or not path.is_file():
        return

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = _parse_env_line(line, path=path, line_number=line_number)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def _parse_env_line(line: str, *, path: Path, line_number: int) -> tuple[str, str] | None:
    """Parse a single dotenv line.

    Supports:
    - blank lines and comments
    - optional `export KEY=...`
    - single-quoted values
    - double-quoted values with standard escape decoding
    - unquoted values with trailing inline comments
    """

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        raise ValueError(f"Invalid dotenv entry in {path}:{line_number}")

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not ENV_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"Invalid dotenv key '{key}' in {path}:{line_number}")

    value = raw_value.strip()
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise ValueError(f"Unterminated double-quoted value in {path}:{line_number}")
        value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"Unterminated single-quoted value in {path}:{line_number}")
        value = value[1:-1]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

    return key, os.path.expandvars(value)


@dataclass(slots=True)
class WebhookConfig:
    """Configuration for a webhook endpoint."""
    
    url: str
    events: list[str] = field(default_factory=list)
    secret: str = ""
    retry_count: int = 3
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebhookConfig":
        """Create from dictionary."""
        return cls(
            url=data.get("url", ""),
            events=data.get("events", []),
            secret=data.get("secret", ""),
            retry_count=data.get("retry_count", 3),
        )


@dataclass(slots=True)
class AgentMailSettings:
    """Configuration for AgentMail email provider."""

    base_url: str = "https://api.agentmail.to"
    api_key: str = ""
    default_inbox_id: str = ""
    poll_interval_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(slots=True)
class OpenClawHookSettings:
    """Configuration for direct OpenClaw notification delivery."""

    base_url: str = ""
    token: str = ""
    gateway_url: str = ""
    gateway_token: str = ""
    agent_id: str | None = None
    timeout_seconds: float = 120.0

    @property
    def hooks_enabled(self) -> bool:
        return bool(self.base_url and self.token)

    @property
    def enabled(self) -> bool:
        return bool(self.hooks_enabled or self.resolved_gateway_url)

    @property
    def resolved_gateway_url(self) -> str:
        candidate = (self.gateway_url or self.base_url).strip()
        if not candidate:
            return ""
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            scheme = "wss" if parsed.scheme == "https" else "ws"
            parsed = parsed._replace(scheme=scheme)
        elif not parsed.scheme:
            parsed = urlparse(f"ws://{candidate}")
        return urlunparse(parsed._replace(params="", query="", fragment=""))

    @property
    def resolved_gateway_token(self) -> str:
        return (self.gateway_token or self.token).strip()


@dataclass(slots=True)
class Settings:
    """Runtime settings for the API service and CLI."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path("~/.local/share/cyborg")
    config_dir: Path = Path("~/.config/cyborg")
    db_path: Path | None = None
    log_path: Path | None = None
    log_level: str = "info"
    debug: bool = False
    version: str = "0.2.0"  # Application version
    pool_size: int = DEFAULT_POOL_SIZE
    webhooks: dict[str, WebhookConfig] = field(default_factory=dict)
    openclaw: OpenClawHookSettings = field(default_factory=OpenClawHookSettings)
    agentmail: AgentMailSettings = field(default_factory=AgentMailSettings)
    email_polling_enabled: bool = True
    heartbeat_interval_seconds: float = 60.0
    projects_base_dir: Path = Path("~/.openclaw/workspace/projects")
    public_url: str = ""  # Public URL for callbacks (e.g., http://localhost:8420)
    dashboard_secret: str = ""  # Shared secret for dashboard-only operations
    dispatch_shutdown_timeout_seconds: float = 30.0
    dispatch_stuck_timeout_minutes: float = 60.0
    dispatch_concurrency_limit: int = 10

    @property
    def dashboard_secret_configured(self) -> bool:
        return bool(self.dashboard_secret.strip())

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser()
        self.config_dir = self.config_dir.expanduser()
        self.projects_base_dir = self.projects_base_dir.expanduser()
        if self.db_path is None:
            self.db_path = self.data_dir / "cyborg.db"
        else:
            self.db_path = self.db_path.expanduser()
        if self.log_path is not None:
            self.log_path = self.log_path.expanduser()

    @property
    def resolved_public_url(self) -> str:
        """Get the public URL, falling back to host:port if not set."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""

        _load_cyborg_env_files()
        data_dir = _env_path("CYBORG_DATA_DIR", Path("~/.local/share/cyborg"))
        config_dir = _env_path("CYBORG_CONFIG_DIR", Path("~/.config/cyborg"))
        db_path_value = os.getenv("CYBORG_DB_PATH")
        db_path = Path(db_path_value).expanduser() if db_path_value else data_dir / "cyborg.db"
        host = os.getenv("CYBORG_HOST", DEFAULT_HOST)
        port = int(os.getenv("CYBORG_PORT", str(DEFAULT_PORT)))
        pool_size = int(os.getenv("CYBORG_DB_POOL_SIZE", str(DEFAULT_POOL_SIZE)))
        log_level = os.getenv("CYBORG_LOG_LEVEL", "info")
        # Backward compat: read old env var if new one not set
        _old_interval = os.getenv("CYBORG_NOTIFICATION_DISPATCH_INTERVAL_SECONDS", "")
        heartbeat_interval_seconds = float(
            os.getenv("CYBORG_HEARTBEAT_INTERVAL_SECONDS", _old_interval or "60")
        )

        # Logging settings
        log_path_value = os.getenv("CYBORG_LOG_PATH")
        log_path = Path(log_path_value).expanduser() if log_path_value else None
        debug = os.getenv("CYBORG_DEBUG", "").lower() in ("true", "1", "yes", "on")

        # Parse webhook configuration from environment
        webhooks: dict[str, WebhookConfig] = {}
        
        # CYBORG_WEBHOOK_OPENCLAW_URL=http://127.0.0.1:8080/webhook/cyborg
        # CYBORG_WEBHOOK_OPENCLAW_SECRET=secret
        # CYBORG_WEBHOOK_OPENCLAW_EVENTS=task.completed,task.failed,project.blocked
        webhook_prefix = "CYBORG_WEBHOOK_"
        webhook_configs: dict[str, dict[str, Any]] = {}
        
        for key, value in os.environ.items():
            if key.startswith(webhook_prefix):
                # Parse CYBORG_WEBHOOK_{NAME}_{SETTING}
                parts = key[len(webhook_prefix):].lower().split("_")
                if len(parts) >= 2:
                    name = parts[0]
                    setting = "_".join(parts[1:])
                    if name not in webhook_configs:
                        webhook_configs[name] = {}
                    webhook_configs[name][setting] = value
        
        for name, config_data in webhook_configs.items():
            events_str = config_data.get("events", "")
            events = [e.strip() for e in events_str.split(",") if e.strip()]
            webhooks[name] = WebhookConfig(
                url=config_data.get("url", ""),
                events=events,
                secret=config_data.get("secret", ""),
                retry_count=int(config_data.get("retry_count", "3")),
            )

        openclaw = OpenClawHookSettings(
            base_url=os.getenv("CYBORG_OPENCLAW_BASE_URL", "").rstrip("/"),
            token=os.getenv("CYBORG_OPENCLAW_TOKEN", ""),
            gateway_url=os.getenv("CYBORG_OPENCLAW_GATEWAY_URL", "").rstrip("/"),
            gateway_token=os.getenv("CYBORG_OPENCLAW_GATEWAY_TOKEN", ""),
            agent_id=os.getenv("CYBORG_OPENCLAW_AGENT_ID") or None,
            timeout_seconds=float(os.getenv("CYBORG_OPENCLAW_TIMEOUT_SECONDS", "120")),
        )
        public_url = os.getenv("CYBORG_PUBLIC_URL", "")
        dashboard_secret = os.getenv("CYBORG_DASHBOARD_SECRET", "")
        projects_base_dir = _env_path("CYBORG_PROJECTS_BASE_DIR", Path("~/.openclaw/workspace/projects"))

        agentmail = AgentMailSettings(
            base_url=os.getenv("CYBORG_AGENTMAIL_BASE_URL", "https://api.agentmail.to").rstrip("/"),
            api_key=os.getenv("CYBORG_AGENTMAIL_API_KEY", ""),
            default_inbox_id=os.getenv("CYBORG_AGENTMAIL_DEFAULT_INBOX_ID", ""),
            poll_interval_seconds=float(os.getenv("CYBORG_AGENTMAIL_POLL_INTERVAL_SECONDS", "30")),
        )
        email_polling_enabled = os.getenv("CYBORG_EMAIL_POLLING_ENABLED", "true").lower() in ("true", "1", "yes", "on")

        dispatch_shutdown_timeout_seconds = float(
            os.getenv("CYBORG_DISPATCH_SHUTDOWN_TIMEOUT_SECONDS", "30")
        )
        dispatch_stuck_timeout_minutes = float(
            os.getenv("CYBORG_DISPATCH_STUCK_TIMEOUT_MINUTES", "60")
        )
        dispatch_concurrency_limit = int(
            os.getenv("CYBORG_DISPATCH_CONCURRENCY_LIMIT", "10")
        )

        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=db_path,
            log_path=log_path,
            log_level=log_level,
            debug=debug,
            pool_size=pool_size,
            webhooks=webhooks,
            openclaw=openclaw,
            agentmail=agentmail,
            email_polling_enabled=email_polling_enabled,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            projects_base_dir=projects_base_dir,
            public_url=public_url,
            dashboard_secret=dashboard_secret,
            dispatch_shutdown_timeout_seconds=dispatch_shutdown_timeout_seconds,
            dispatch_stuck_timeout_minutes=dispatch_stuck_timeout_minutes,
            dispatch_concurrency_limit=dispatch_concurrency_limit,
        )

    def ensure_directories(self) -> None:
        """Create the configured data and config directories."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
