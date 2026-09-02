"""Structured JSON logging with correlation IDs for Bob.

Configured once at startup via ``configure_logging(settings)`` from
``main.create_app``. After that, all modules just use the standard
``logging.getLogger(__name__)`` and inherit JSON formatting plus the
correlation-id context middleware.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bob_server.config import Settings


# Correlation ID context variable (thread-safe for asyncio)
_correlation_id_context: dict[str, str] = {}


class StructuredFormatter(logging.Formatter):
    """Format log records as JSON with consistent fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        # Base log entry
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add correlation ID if available
        correlation_id = _correlation_id_context.get("current_id")
        if correlation_id:
            log_entry["correlation_id"] = correlation_id

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info),
            }

        # Add any extra fields from record
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "lineno", "funcName", "created", "msecs",
                "relativeCreated", "thread", "threadName", "processName",
                "exc_info", "stack_info",
            } and not key.startswith("_"):
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


def set_correlation_id(correlation_id: str | None = None) -> str:
    """Set correlation ID for a current context.

    Args:
        correlation_id: Correlation ID to use. If None, generates a new UUID.

    Returns:
        The correlation ID that was set
    """
    if correlation_id is None:
        correlation_id = str(uuid.uuid4())
    _correlation_id_context["current_id"] = correlation_id
    return correlation_id


def clear_correlation_id() -> None:
    """Clear the correlation ID from context."""
    _correlation_id_context.pop("current_id", None)


# ============================================================================
# Helper Functions
# ============================================================================


def log_metric(
    logger: logging.Logger,
    metric_name: str,
    metric_value: float | int,
    unit: str | None = None,
    **extra: Any,
) -> None:
    """Log a metric value.

    Args:
        logger: Logger instance
        metric_name: Name of metric
        metric_value: Value of metric
        unit: Unit of measurement (optional)
        **extra: Additional fields to include
    """
    log_data: dict[str, Any] = {
        "event_type": "metric",
        "metric_name": metric_name,
        "metric_value": metric_value,
    }

    if unit:
        log_data["unit"] = unit

    log_data.update(extra)

    logger.info(f"Metric: {metric_name}", extra=log_data)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure root logging for application.

    Args:
        settings: Application settings (uses Settings.from_env() if None)
    """
    settings = settings or Settings.from_env()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Add structured console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(console_handler)

    # Optionally add file handler
    if settings.log_path:
        log_path = Path(settings.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(StructuredFormatter())
        root_logger.addHandler(file_handler)

    if settings.log_dir:
        try:
            rolling_handler = DailyRollingFileHandler(settings.log_dir, "bob-server")
            rolling_handler.setFormatter(StructuredFormatter())
            root_logger.addHandler(rolling_handler)
        except Exception:
            # Don't let a bad log dir prevent startup; console still works.
            logging.getLogger(__name__).exception(
                "failed to add DailyRollingFileHandler for log_dir=%s", settings.log_dir
            )

    # Quieten noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ============================================================================
# Daily Rolling File Handler
# ============================================================================


_DATE_SOURCE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)\.log$")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _archive_previous_days(log_dir: Path, source: str, today: str) -> None:
    """Move any {date}_{source}.log files in log_dir (where date != today) into log_dir/older/."""

    older_dir = log_dir / "older"
    older_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{source}.log"
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        match = _DATE_SOURCE_RE.match(entry.name)
        if not match or match.group(2) != source:
            continue
        date_str = match.group(1)
        if date_str == today:
            continue
        target = older_dir / entry.name
        try:
            os.replace(entry, target)
        except OSError:
            pass


class DailyRollingFileHandler(logging.Handler):
    """Append to `log_dir/{YYYY-MM-DD}_{source}.log`, rotating at local midnight.

    On rollover the previous day's file is moved into `log_dir/older/`. Any stale
    files from earlier days are also archived on startup so a service that was
    down across a date boundary doesn't leave old files in the root.
    """

    def __init__(self, log_dir: Path, source: str) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.source = source
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_date = _today_str()
        _archive_previous_days(self.log_dir, self.source, self._current_date)
        self._current_path = self.log_dir / f"{self._current_date}_{self.source}.log"
        self._fh = open(self._current_path, "a", encoding="utf-8")

    def _maybe_rotate(self) -> None:
        today = _today_str()
        if today == self._current_date:
            return
        try:
            self._fh.flush()
            self._fh.close()
        except OSError:
            pass
        older_dir = self.log_dir / "older"
        older_dir.mkdir(parents=True, exist_ok=True)
        target = older_dir / self._current_path.name
        try:
            os.replace(self._current_path, target)
        except OSError:
            pass
        self._current_date = today
        self._current_path = self.log_dir / f"{today}_{self.source}.log"
        self._fh = open(self._current_path, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._maybe_rotate()
                self._fh.write(msg + "\n")
                self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            with self._lock:
                self._fh.close()
        except Exception:
            pass
        super().close()


class CorrelationIdMiddleware:
    """Starlette middleware to add correlation IDs to requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Generate or extract correlation ID
            headers = dict(scope.get("headers", []))
            correlation_id = headers.get(b"x-correlation-id", b"").decode() or str(uuid.uuid4())

            # Store in context
            _correlation_id_context["correlation_id"] = correlation_id

            # Add to response headers
            async def send_with_header(message):
                if message["type"] == "http.response.start":
                    headers_list = list(message.get("headers", []))
                    headers_list.append((b"x-correlation-id", correlation_id.encode()))
                    message["headers"] = headers_list
                await send(message)

            await self.app(scope, receive, send_with_header)
        else:
            await self.app(scope, receive, send)