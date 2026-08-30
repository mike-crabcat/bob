"""Configuration helpers for the Bob service."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import secrets

logger = logging.getLogger(__name__)
import re
import subprocess
import sys
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_POOL_SIZE = 4
DEFAULT_ENV_FILE_NAME = ".env"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var; unset falls back to the default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default.expanduser()


def _load_bob_env_files() -> None:
    """Load `.env` files into the process environment without overriding explicit env vars.

    Precedence:
    1. Existing process environment
    2. `BOB_ENV_FILE`, if set
    3. `.env` in the current working directory
    4. `.env` in the resolved config directory
    """

    candidates: list[Path] = []
    explicit_env_file = os.getenv("BOB_ENV_FILE")
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser())

    candidates.append(Path.cwd() / DEFAULT_ENV_FILE_NAME)

    for path in candidates:
        _load_env_file(path)

    config_dir = _env_path("BOB_CONFIG_DIR", Path("~/config"))
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
class HomeAssistantSettings:
    """Configuration for Home Assistant integration (pull-based location queries).

    The Companion app on the user's phone publishes GPS to HA as a
    ``device_tracker.*`` entity. Bob queries HA's REST API on demand via the
    ``current_location()`` tool — see services/homeassistant_client.py and
    services/location_tools.py.
    """

    enabled: bool = False
    url: str = ""
    bearer_token: str = ""
    device_tracker_entity_id: str = ""
    # Scheduled background fetcher — appends a row to location_history every
    # history_interval_seconds. See LocationFetchTask in heartbeat.py.
    history_enabled: bool = True
    history_interval_seconds: int = 900


@dataclass(slots=True)
class VoiceSettings:
    """Configuration for realtime voice surfaces (frontend + router gate)."""

    enabled: bool = True
    frontend_dir: Path | None = None


@dataclass(slots=True)
class PhoneSettings:
    """Configuration for the phone/telephony subsystem (Twilio)."""

    enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    base_url: str = ""
    # Twilio home region (e.g. "au1"). Empty = default us1. Our account is
    # us1, so control+media process in Ashburn despite AU-only calls — every
    # round trip crosses the Pacific (webhooks observed from AWS Virginia,
    # 2026-08-18). Migrating to an au1 project moves media to Sydney
    # (~60ms from this box); needs an au1-hosted number first — see
    # Twilio Regional migration docs.
    twilio_region: str = ""
    silence_threshold: float = 0.01
    silence_duration: float = 1.5
    call_recording_enabled: bool = True
    call_recording_max_age_days: int = 30


@dataclass(slots=True)
class OpenAISettings:
    """Configuration for direct OpenAI LLM API access."""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    default_model: str = "gpt-5.6-sol"
    memory_model: str = ""
    timeout_seconds: float = 120.0
    web_search_enabled: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_memory_model(self) -> str:
        return self.memory_model or self.default_model


@dataclass(slots=True)
class OpenRouterSettings:
    """Configuration for OpenRouter — the multi-provider LLM gateway used
    for non-OpenAI models (vendor-qualified slugs like ``z-ai/glm-5.3-flash``,
    see services/model_registry.py).

    OpenRouter implements the OpenAI Responses API, so it reuses the same
    request path as OpenAISettings — only the client endpoint differs.
    The API key is read from a file (the BOB_HA_BEARER_TOKEN_FILE pattern)
    rather than the environment: the agent's bash tool inherits the service
    env, and this key shouldn't be readable there.
    """

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(slots=True)
class OpenAIRealtimeSettings:
    """Configuration for OpenAI Realtime API voice calls.

    Reuses ``openai.api_key`` (same OpenAI account). The Realtime bridge is
    audio-source-agnostic — see services/realtime_bridge.py — so the same
    settings serve phone calls (Twilio μ-law) and the browser test harness.
    """

    model: str = "gpt-realtime-2.1"
    voice: str = "cedar"
    max_call_duration_seconds: float = 300.0
    turn_detection: str = "server_vad"
    input_audio_format: str = "pcm16"
    output_audio_format: str = "pcm16"

    # Voices supported by gpt-realtime session.update (rejected otherwise —
    # and a rejected session.update silently runs the call on DEFAULT session
    # settings: no instructions, no tools, wrong voice). TTS-only voices such
    # as fable/nova/onyx are NOT valid here.
    VALID_VOICES = (
        "alloy", "ash", "ballad", "coral", "echo", "sage",
        "shimmer", "verse", "marin", "cedar",
    )

    @property
    def enabled(self) -> bool:
        # Relies on the shared OpenAI key; surfaced via the OpenAISettings at
        # resolution time (realtime needs openai.api_key to be set).
        return True


@dataclass(slots=True)
class HarnessSettings:
    """Configuration for the local LLM harness for voice/phone."""

    enabled: bool = False
    workspace_dir: Path = Path("~/workspace")
    venv_dir: Path = Path("~/bobenv")
    default_model: str = "gpt-5.6-sol"
    max_history_messages: int = 20
    skill_dev_enabled: bool = False
    skill_dev_max_budget_usd: float = 5.0
    skill_dev_timeout_seconds: float = 1800.0
    local_subagent_model: str = "gpt-5.6-sol"


@dataclass(slots=True)
class WhatsAppBridgeSettings:
    """Configuration for the WhatsApp bridge companion service."""

    enabled: bool = False
    url: str = "ws://127.0.0.1:8430/ws"
    token: str = ""
    reconnect_interval_seconds: float = 10.0
    media_dir: Path = Path("~/data/whatsappbridge/media")


@dataclass(slots=True)
class PatienceSettings:
    """Configuration for the patience dispatch system."""

    enabled: bool = False
    model: str = "gpt-5.6-luna"
    bot_name: str = "Bot"
    max_pending_items: int = 20
    max_context_messages: int = 10
    # Fixed delay (seconds) used when patience is OFF. Messages still batch
    # through the buffer but skip the LLM gate — the timer is just a short
    # settle window to absorb bursts. Set to 0 to dispatch ASAP.
    patience_off_settle_seconds: float = 1.5


@dataclass(slots=True)
class ReconciliationSettings:
    """Configuration for memory reconciliation model selection.

    large_model_types: entity types (e.g. trip, connection) whose reconciliation
    should use the large model (openai.default_model) instead of the small model
    (openai.memory_model).
    """

    large_model_types: list[str] = field(default_factory=list)
    min_interval_hours: float = 6.0
    daily_batch_enabled: bool = True
    daily_batch_max_entities: int = 50


@dataclass(slots=True)
class DreamSettings:
    """Configuration for the dream system (see dream-v2-plan.md).

    All LLM passes run on the low-cost memory model (openai.get_memory_model()).
    Env values are boot defaults; runtime toggles like auto_approve_plans live in
    the dream_config table and override.
    """

    enabled: bool = False
    interval_minutes: float = 240.0
    min_new_sessions: int = 1
    min_new_messages_per_session: int = 4
    max_sessions_per_run: int = 8
    first_run_lookback_days: int = 14
    backlog_evidence_days: int = 7         # older evidence never auto-approves
    max_new_items_per_type: int = 3
    dedup_distance_threshold: float = 0.25
    draft_mode: bool = True
    auto_approve_plans: bool = False       # boot default; runtime value in dream_config
    announce_daily_cap_per_session: int = 3
    announce_defer_active_minutes: int = 10
    reannounce_after_days: int = 3
    max_reannounces_per_plan: int = 1
    plan_stalled_runs: int = 2
    plan_stale_days: int = 14
    recent_terminal_dedup_days: int = 14
    resolution_kept_consecutive_runs: int = 3
    resolution_stale_runs: int = 5
    max_transcript_lines: int = 120


@dataclass(slots=True)
class GoalsSettings:
    """Configuration for the goal-state reviser (bob-events-plan.md §1.3).

    reviser_model empty → the low-cost memory model (openai.get_memory_model()).
    Shadow mode (revisions run, wakes suppressed and logged) is the
    BOB_GOAL_STATE_SHADOW env kill switch, read at call time so it toggles
    without a restart.
    """

    reviser_model: str = ""
    # Output-token ceiling for one reviser pass. max_output_tokens caps
    # reasoning AND content together on thinking models — 900 let GLM burn
    # the whole budget on reasoning and return empty text (every call
    # degraded to wake until 2026-08-29). 4000 leaves room for low-effort
    # reasoning plus the state JSON.
    reviser_max_tokens: int = 4000
    max_concurrent_revisions: int = 3
    max_cas_retries: int = 3
    # Progress-review loop (§4.1); BOB_GOAL_REVIEW_DISABLED is the runtime
    # kill switch, read at call time.
    review_threshold_hours: float = 24.0


@dataclass(slots=True)
class Settings:
    """Runtime settings for the API service and CLI."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path("~/data")
    config_dir: Path = Path("~/config")
    db_path: Path | None = None
    log_path: Path | None = None
    log_dir: Path | None = None
    log_level: str = "info"
    debug: bool = False
    version: str = "0.2.0"  # Application version
    pool_size: int = DEFAULT_POOL_SIZE
    webhooks: dict[str, WebhookConfig] = field(default_factory=dict)
    agentmail: AgentMailSettings = field(default_factory=AgentMailSettings)
    homeassistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
    email_polling_enabled: bool = True
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    phone: PhoneSettings = field(default_factory=PhoneSettings)
    openai: OpenAISettings = field(default_factory=OpenAISettings)
    openrouter: OpenRouterSettings = field(default_factory=OpenRouterSettings)
    openai_realtime: OpenAIRealtimeSettings = field(default_factory=OpenAIRealtimeSettings)
    harness: HarnessSettings = field(default_factory=HarnessSettings)
    whatsapp_bridge: WhatsAppBridgeSettings = field(default_factory=WhatsAppBridgeSettings)
    patience: PatienceSettings = field(default_factory=PatienceSettings)
    reconciliation: ReconciliationSettings = field(default_factory=ReconciliationSettings)
    dream: DreamSettings = field(default_factory=DreamSettings)
    goals: GoalsSettings = field(default_factory=GoalsSettings)
    heartbeat_interval_seconds: float = 60.0
    public_url: str = ""  # Public URL for callbacks (e.g., http://localhost:8420)
    dashboard_secret: str = ""  # Shared secret for dashboard-only operations
    api_auth_disabled: bool = False  # Kill switch: bypass the API token gate
    _api_secret_cache: str | None = field(default=None, init=False, repr=False, compare=False)
    session_summary_idle_minutes: float = 5.0

    @property
    def dashboard_secret_configured(self) -> bool:
        return bool(self.dashboard_secret.strip())

    @property
    def api_auth_enabled(self) -> bool:
        return not self.api_auth_disabled

    @property
    def api_secret_path(self) -> Path:
        return self.data_dir / "api_secret"

    @property
    def resolved_api_secret(self) -> str:
        """The API token: explicit BOB_DASHBOARD_SECRET if set, else a generated
        secret persisted under data_dir so it is stable across restarts.

        Kept out of os.environ on purpose — the agent bash tool inherits the
        server environment, so a secret set in `.env` would be readable via
        printenv. The file lives outside the workspace.
        """
        if self.dashboard_secret_configured:
            return self.dashboard_secret.strip()
        if self._api_secret_cache:
            return self._api_secret_cache
        secret = ""
        try:
            secret = self.api_secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not secret:
            secret = secrets.token_urlsafe(32)
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.api_secret_path.write_text(secret + "\n", encoding="utf-8")
                os.chmod(self.api_secret_path, 0o600)
            except OSError:
                logger.warning(
                    "Cannot persist API secret to %s; it will change on restart",
                    self.api_secret_path,
                )
        self._api_secret_cache = secret
        return secret

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser()
        self.config_dir = self.config_dir.expanduser()
        if self.db_path is None:
            self.db_path = self.data_dir / "bob.db"
        else:
            self.db_path = self.db_path.expanduser()
        if self.log_path is not None:
            self.log_path = self.log_path.expanduser()
        if self.log_dir is not None:
            self.log_dir = self.log_dir.expanduser()

    @property
    def resolved_public_url(self) -> str:
        """Get the public URL, falling back to host:port if not set."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""

        _load_bob_env_files()
        data_dir = _env_path("BOB_DATA_DIR", Path("~/data"))
        config_dir = _env_path("BOB_CONFIG_DIR", Path("~/config"))
        db_path_value = os.getenv("BOB_DB_PATH")
        db_path = Path(db_path_value).expanduser() if db_path_value else data_dir / "bob.db"
        host = os.getenv("BOB_HOST", DEFAULT_HOST)
        port = int(os.getenv("BOB_PORT", str(DEFAULT_PORT)))
        pool_size = int(os.getenv("BOB_DB_POOL_SIZE", str(DEFAULT_POOL_SIZE)))
        log_level = os.getenv("BOB_LOG_LEVEL", "info")
        heartbeat_interval_seconds = float(
            os.getenv("BOB_HEARTBEAT_INTERVAL_SECONDS", "60")
        )

        # Logging settings
        log_path_value = os.getenv("BOB_LOG_PATH")
        log_path = Path(log_path_value).expanduser() if log_path_value else None
        log_dir = _env_path("BOB_LOG_DIR", Path("~/logs"))
        debug = os.getenv("BOB_DEBUG", "").lower() in ("true", "1", "yes", "on")

        # Parse webhook configuration from environment
        webhooks: dict[str, WebhookConfig] = {}
        
        # BOB_WEBHOOK_EXAMPLE_URL=http://127.0.0.1:8080/webhook
        # BOB_WEBHOOK_EXAMPLE_SECRET=secret
        # BOB_WEBHOOK_EXAMPLE_EVENTS=message.created,message.failed
        webhook_prefix = "BOB_WEBHOOK_"
        webhook_configs: dict[str, dict[str, Any]] = {}

        for key, value in os.environ.items():
            if key.startswith(webhook_prefix):
                # Parse BOB_WEBHOOK_{NAME}_{SETTING}
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

        public_url = os.getenv("BOB_PUBLIC_URL", "")
        dashboard_secret = os.getenv("BOB_DASHBOARD_SECRET", "")
        api_auth_disabled = _env_bool("BOB_API_AUTH_DISABLED", False)

        agentmail = AgentMailSettings(
            base_url=os.getenv("BOB_AGENTMAIL_BASE_URL", "https://api.agentmail.to").rstrip("/"),
            api_key=os.getenv("BOB_AGENTMAIL_API_KEY", ""),
            default_inbox_id=os.getenv("BOB_AGENTMAIL_DEFAULT_INBOX_ID", ""),
            poll_interval_seconds=float(os.getenv("BOB_AGENTMAIL_POLL_INTERVAL_SECONDS", "30")),
        )

        # Home Assistant token: read directly from env, or from a file path
        # supplied via BOB_HA_BEARER_TOKEN_FILE (avoids putting the secret in
        # the environment directly).
        ha_token = os.getenv("BOB_HA_BEARER_TOKEN", "")
        ha_token_file = os.getenv("BOB_HA_BEARER_TOKEN_FILE", "")
        if not ha_token and ha_token_file:
            try:
                ha_token = Path(ha_token_file).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                ha_token = ""

        ha_url = os.getenv("BOB_HA_URL", "").rstrip("/")
        ha_entity = os.getenv("BOB_HA_DEVICE_TRACKER_ENTITY_ID", "")
        ha_explicitly_enabled = os.getenv("BOB_HA_ENABLED", "").lower() in ("true", "1", "yes", "on")
        # Auto-enable when fully configured even if BOB_HA_ENABLED is unset.
        ha_enabled = ha_explicitly_enabled or bool(ha_url and ha_token and ha_entity)
        homeassistant = HomeAssistantSettings(
            enabled=ha_enabled,
            url=ha_url,
            bearer_token=ha_token,
            device_tracker_entity_id=ha_entity,
            history_enabled=os.getenv("BOB_HA_HISTORY_ENABLED", "true").lower()
                in ("true", "1", "yes", "on"),
            history_interval_seconds=int(os.getenv("BOB_HA_HISTORY_INTERVAL_SECONDS", "900")),
        )
        email_polling_enabled = os.getenv("BOB_EMAIL_POLLING_ENABLED", "true").lower() in ("true", "1", "yes", "on")

        voice = VoiceSettings(
            enabled=os.getenv("BOB_VOICE_ENABLED", "true").lower() not in ("false", "0", "no", "off"),
            frontend_dir=Path(v).expanduser() if (v := os.getenv("BOB_VOICE_FRONTEND_DIR")) else None,
        )

        session_summary_idle_minutes = float(
            os.getenv("BOB_SESSION_SUMMARY_IDLE_MINUTES", "5.0")
        )

        phone = PhoneSettings(
            enabled=os.getenv("BOB_PHONE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            twilio_account_sid=os.getenv("BOB_PHONE_TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("BOB_PHONE_TWILIO_AUTH_TOKEN", ""),
            twilio_phone_number=os.getenv("BOB_PHONE_TWILIO_PHONE_NUMBER", ""),
            twilio_region=os.getenv("BOB_PHONE_TWILIO_REGION", ""),
            base_url=os.getenv("BOB_PHONE_BASE_URL", ""),
            silence_threshold=float(os.getenv("BOB_PHONE_SILENCE_THRESHOLD", "0.01")),
            silence_duration=float(os.getenv("BOB_PHONE_SILENCE_DURATION", "1.5")),
            call_recording_enabled=os.getenv("BOB_PHONE_CALL_RECORDING_ENABLED", "true").lower() in ("true", "1", "yes", "on"),
            call_recording_max_age_days=int(os.getenv("BOB_PHONE_CALL_RECORDING_MAX_AGE_DAYS", "30")),
        )

        openai_llm = OpenAISettings(
            api_key=os.getenv("BOB_OPENAI_API_KEY", ""),
            base_url=os.getenv("BOB_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            default_model=os.getenv("BOB_OPENAI_DEFAULT_MODEL", "gpt-5.6-sol"),
            memory_model=os.getenv("BOB_OPENAI_MEMORY_MODEL", ""),
            timeout_seconds=float(os.getenv("BOB_OPENAI_TIMEOUT_SECONDS", "120")),
            web_search_enabled=os.getenv("BOB_OPENAI_WEB_SEARCH", "").lower() in ("1", "true", "yes"),
        )

        # OpenRouter key: from a file by default (BOB_HA_BEARER_TOKEN_FILE
        # pattern) to keep it out of the environment the agent's bash inherits.
        openrouter_key_file = os.getenv(
            "BOB_OPENROUTER_API_KEY_FILE", str(config_dir / "openrouter_api_key"))
        try:
            openrouter_key = Path(openrouter_key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            openrouter_key = ""
        openrouter = OpenRouterSettings(
            api_key=openrouter_key,
            base_url=os.getenv("BOB_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        )

        realtime_voice = os.getenv("BOB_OPENAI_REALTIME_VOICE", "cedar")
        if realtime_voice not in OpenAIRealtimeSettings.VALID_VOICES:
            logger.warning(
                "BOB_OPENAI_REALTIME_VOICE=%r is not a valid Realtime voice %s — "
                "falling back to 'cedar' (an invalid voice makes session.update "
                "fail and the call runs on default session settings)",
                realtime_voice, OpenAIRealtimeSettings.VALID_VOICES,
            )
            realtime_voice = "cedar"

        openai_realtime = OpenAIRealtimeSettings(
            model=os.getenv("BOB_OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
            voice=realtime_voice,
            max_call_duration_seconds=float(os.getenv("BOB_OPENAI_REALTIME_MAX_DURATION", "300")),
            turn_detection=os.getenv("BOB_OPENAI_REALTIME_TURN_DETECTION", "server_vad"),
        )

        harness = HarnessSettings(
            enabled=os.getenv("BOB_HARNESS_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            workspace_dir=_env_path("BOB_HARNESS_WORKSPACE_DIR", Path("~/workspace")),
            venv_dir=_env_path("BOB_HARNESS_VENV_DIR", Path("~/bobenv")),
            default_model=os.getenv("BOB_HARNESS_DEFAULT_MODEL") or os.getenv("BOB_OPENAI_DEFAULT_MODEL", "gpt-5.6-sol"),
            max_history_messages=int(os.getenv("BOB_HARNESS_MAX_HISTORY_MESSAGES", "20")),
            skill_dev_enabled=os.getenv("BOB_HARNESS_SKILL_DEV_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            skill_dev_max_budget_usd=float(os.getenv("BOB_HARNESS_SKILL_DEV_MAX_BUDGET_USD", "5.0")),
            skill_dev_timeout_seconds=float(os.getenv("BOB_HARNESS_SKILL_DEV_TIMEOUT_SECONDS", "1800")),
            local_subagent_model=os.getenv("BOB_HARNESS_LOCAL_SUBAGENT_MODEL", "gpt-5.6-sol"),
        )

        whatsapp_bridge = WhatsAppBridgeSettings(
            enabled=os.getenv("BOB_WHATSAPP_BRIDGE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            url=os.getenv("BOB_WHATSAPP_BRIDGE_URL", "ws://127.0.0.1:8430/ws"),
            token=os.getenv("BOB_WHATSAPP_BRIDGE_TOKEN", ""),
            reconnect_interval_seconds=float(os.getenv("BOB_WHATSAPP_BRIDGE_RECONNECT_INTERVAL_SECONDS", "10")),
            media_dir=_env_path("BOB_WHATSAPP_BRIDGE_MEDIA_DIR", Path("~/data/whatsappbridge/media")),
        )

        patience = PatienceSettings(
            enabled=os.getenv("BOB_PATIENCE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            model=os.getenv("BOB_PATIENCE_MODEL", "gpt-5.6-luna"),
            bot_name=os.getenv("BOB_SELF_NAME", "Bob"),
            max_pending_items=int(os.getenv("BOB_PATIENCE_MAX_PENDING", "20")),
            max_context_messages=int(os.getenv("BOB_PATIENCE_MAX_CONTEXT", "10")),
            patience_off_settle_seconds=float(os.getenv("BOB_PATIENCE_OFF_SETTLE_SECONDS", "1.5")),
        )

        recon_large_types_raw = os.getenv("BOB_RECON_LARGE_MODEL_TYPES", "")
        recon_large_types = [t.strip() for t in recon_large_types_raw.split(",") if t.strip()]
        reconciliation = ReconciliationSettings(
            large_model_types=recon_large_types,
            min_interval_hours=float(os.getenv("BOB_RECON_MIN_INTERVAL_HOURS", "6.0")),
            daily_batch_enabled=os.getenv("BOB_RECON_DAILY_BATCH_ENABLED", "1").strip() not in ("0", "false", "no"),
            daily_batch_max_entities=int(os.getenv("BOB_RECON_DAILY_BATCH_MAX_ENTITIES", "50")),
        )

        dream = DreamSettings(
            enabled=_env_bool("BOB_DREAM_ENABLED"),
            interval_minutes=float(os.getenv("BOB_DREAM_INTERVAL_MINUTES", "240")),
            min_new_sessions=int(os.getenv("BOB_DREAM_MIN_NEW_SESSIONS", "1")),
            min_new_messages_per_session=int(os.getenv("BOB_DREAM_MIN_NEW_MESSAGES", "4")),
            max_sessions_per_run=int(os.getenv("BOB_DREAM_MAX_SESSIONS_PER_RUN", "8")),
            first_run_lookback_days=int(os.getenv("BOB_DREAM_FIRST_RUN_LOOKBACK_DAYS", "14")),
            backlog_evidence_days=int(os.getenv("BOB_DREAM_BACKLOG_EVIDENCE_DAYS", "7")),
            max_new_items_per_type=int(os.getenv("BOB_DREAM_MAX_NEW_ITEMS_PER_TYPE", "3")),
            dedup_distance_threshold=float(os.getenv("BOB_DREAM_DEDUP_THRESHOLD", "0.25")),
            draft_mode=_env_bool("BOB_DREAM_DRAFT_MODE", default=True),
            auto_approve_plans=_env_bool("BOB_DREAM_AUTO_APPROVE_PLANS"),
            announce_daily_cap_per_session=int(os.getenv("BOB_DREAM_ANNOUNCE_DAILY_CAP", "3")),
            announce_defer_active_minutes=int(os.getenv("BOB_DREAM_ANNOUNCE_DEFER_ACTIVE_MINUTES", "10")),
            reannounce_after_days=int(os.getenv("BOB_DREAM_REANNOUNCE_AFTER_DAYS", "3")),
            max_reannounces_per_plan=int(os.getenv("BOB_DREAM_MAX_REANNOUNCES", "1")),
            plan_stalled_runs=int(os.getenv("BOB_DREAM_PLAN_STALLED_RUNS", "2")),
            plan_stale_days=int(os.getenv("BOB_DREAM_PLAN_STALE_DAYS", "14")),
            recent_terminal_dedup_days=int(os.getenv("BOB_DREAM_RECENT_TERMINAL_DEDUP_DAYS", "14")),
            resolution_kept_consecutive_runs=int(os.getenv("BOB_DREAM_RESOLUTION_KEPT_RUNS", "3")),
            resolution_stale_runs=int(os.getenv("BOB_DREAM_RESOLUTION_STALE_RUNS", "5")),
            max_transcript_lines=int(os.getenv("BOB_DREAM_MAX_TRANSCRIPT_LINES", "120")),
        )

        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=db_path,
            log_path=log_path,
            log_dir=log_dir,
            log_level=log_level,
            debug=debug,
            pool_size=pool_size,
            webhooks=webhooks,
            agentmail=agentmail,
            homeassistant=homeassistant,
            email_polling_enabled=email_polling_enabled,
            voice=voice,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            public_url=public_url,
            dashboard_secret=dashboard_secret,
            api_auth_disabled=api_auth_disabled,
            session_summary_idle_minutes=session_summary_idle_minutes,
            phone=phone,
            openai=openai_llm,
            openrouter=openrouter,
            openai_realtime=openai_realtime,
            harness=harness,
            whatsapp_bridge=whatsapp_bridge,
            patience=patience,
            reconciliation=reconciliation,
            dream=dream,
            goals=GoalsSettings(
                reviser_model=os.getenv("BOB_GOALS_REVISER_MODEL", ""),
                reviser_max_tokens=int(os.getenv("BOB_GOALS_REVISER_MAX_TOKENS", "4000")),
                max_concurrent_revisions=int(os.getenv("BOB_GOALS_MAX_CONCURRENT_REVISIONS", "3")),
                max_cas_retries=int(os.getenv("BOB_GOALS_MAX_CAS_RETRIES", "3")),
                review_threshold_hours=float(os.getenv("BOB_GOAL_REVIEW_THRESHOLD_HOURS", "24")),
            ),
        )

    def ensure_directories(self) -> None:
        """Create the configured data and config directories."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if self.phone.enabled:
            (self.data_dir / "calls").mkdir(parents=True, exist_ok=True)
        self._ensure_venv()

    def _ensure_venv(self) -> None:
        """Create the harness venv if missing. Non-fatal on failure.

        Skills and bash-spawned scripts install into this venv. If creation
        fails, we leave it missing — build_skill_env guards on <venv>/bin/python
        and falls back to system PATH, so the bash tool still works.
        """
        log = logging.getLogger(__name__)
        venv = self.harness.venv_dir.expanduser()
        if (venv / "bin" / "python").exists():
            return
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                check=False,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            log.warning("Failed to create harness venv at %s: %s", venv, e)
            return
        if not (venv / "bin" / "python").exists():
            log.warning("Harness venv creation did not produce a python binary at %s", venv)
