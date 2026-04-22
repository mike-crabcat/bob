"""Structured JSON logging with correlation IDs for Cyborg.

This module provides structured logging utilities that output JSON-formatted logs
with consistent fields including correlation IDs for request tracking.

Usage:
    from cyborg_core.structured_logging import get_logger, log_reasoning_request

    logger = get_logger(__name__)
    logger.info("Task completed", extra={"task_id": "abc-123", "project_id": "def-456"})
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from cyborg_core.config import Settings


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


def get_logger(name: str) -> logging.Logger:
    """Get a logger with structured formatting.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance configured for structured JSON output
    """
    logger = logging.getLogger(name)

    # Only configure if not already configured
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Don't propagate to root logger to avoid duplicate logs
        logger.propagate = False

    return logger


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
# Decorators for automatic logging
# ============================================================================


def log_execution(
    logger: logging.Logger | None = None,
    event_name: str | None = None,
    log_args: bool = False,
    log_result: bool = False,
    log_errors: bool = True,
) -> Callable:
    """Decorator to log function execution with timing.

    Args:
        logger: Logger instance (uses module logger if None)
        event_name: Name for event (uses function name if None)
        log_args: Whether to log function arguments
        log_result: Whether to log function return value
        log_errors: Whether to log errors

    Returns:
        Decorated function
    """
    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        fn_logger = logger or get_logger(func.__module__)
        name = event_name or f"{func.__module__}.{func.__name__}"
        start_time = datetime.now(timezone.utc)

        log_data: dict[str, Any] = {
            "event_type": "function_call",
            "function": name,
        }

        if log_args:
            log_data["args"] = str(args)[:500]
            log_data["kwargs"] = str(list(kwargs.keys()))

        fn_logger.info(f"Calling {name}", extra=log_data)

        try:
            result = await func(*args, **kwargs)
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()

            completion_data: dict[str, Any] = {
                "event_type": "function_return",
                "function": name,
                "duration_seconds": round(duration, 3),
                "success": True,
            }

            if log_result:
                completion_data["result"] = str(result)[:500]

            fn_logger.info(f"Completed {name}", extra=completion_data)
            return result

        except Exception as e:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()

            if log_errors:
                error_data: dict[str, Any] = {
                    "event_type": "function_error",
                    "function": name,
                    "duration_seconds": round(duration, 3),
                    "error_type_name": type(e).__name__,
                    "error_message": str(e),
                }
                fn_logger.error(f"Error in {name}: {e}", extra=error_data, exc_info=True)

            raise


# ============================================================================
# Helper Functions
# ============================================================================


def log_reasoning_request(
    logger: logging.Logger,
    reasoning_type: str,
    project_id: str | None = None,
    task_id: str | None = None,
    duration_seconds: float | None = None,
    success: bool | None = None,
    error: str | None = None,
    **extra: Any,
) -> None:
    """Log an OpenClaw reasoning request with structured fields.

    Args:
        logger: Logger instance
        reasoning_type: Type of reasoning (plan_generation, evaluation, refinement, etc.)
        project_id: Associated project ID
        task_id: Associated task ID
        duration_seconds: Request duration
        success: Whether the request succeeded
        error: Error message if failed
        **extra: Additional fields to include
    """
    log_data: dict[str, Any] = {
        "event_type": "reasoning_request",
        "reasoning_type": reasoning_type,
    }

    if project_id:
        log_data["project_id"] = project_id
    if task_id:
        log_data["task_id"] = task_id
    if duration_seconds is not None:
        log_data["duration_seconds"] = round(duration_seconds, 3)

    if success is not None:
        log_data["success"] = success
    if error:
        log_data["error"] = error

    log_data.update(extra)

    if success:
        logger.info("Reasoning request completed", extra=log_data)
    elif success is False:
        logger.error("Reasoning request failed", extra=log_data)


def log_autonomy_decision(
    logger: logging.Logger,
    decision_type: str,
    project_id: str,
    **extra: Any,
) -> None:
    """Log an autonomous decision with structured fields.

    Args:
        logger: Logger instance
        decision_type: Type of decision (refinement, follow_up_tasks, completion, etc.)
        project_id: Associated project ID
        **extra: Additional fields to include
    """
    log_data: dict[str, Any] = {
        "event_type": "autonomy_decision",
        "decision_type": decision_type,
        "project_id": project_id,
    }

    log_data.update(extra)

    logger.info(f"Autonomy decision: {decision_type}", extra=log_data)


def log_health_check(
    logger: logging.Logger,
    project_id: str,
    health_score: float,
    risk_level: str,
    **extra: Any,
) -> None:
    """Log a health check result with structured fields.

    Args:
        logger: Logger instance
        project_id: Associated project ID
        health_score: Health score (0-1)
        risk_level: Risk level (low, medium, high, critical)
        **extra: Additional fields to include
    """
    log_data: dict[str, Any] = {
        "event_type": "health_check",
        "project_id": project_id,
        "health_score": health_score,
        "risk_level": risk_level,
    }

    log_data.update(extra)

    if risk_level in ("high", "critical"):
        logger.warning(f"Health check: {risk_level} risk", extra=log_data)
    else:
        logger.info("Health check completed", extra=log_data)


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


# ============================================================================
# Database Log Handler
# ============================================================================


class DatabaseLogHandler(logging.Handler):
    """Log handler that writes logs to a structured_logs table.

    Uses a background thread to avoid blocking the main application.
    """

    def __init__(self, buffer_size: int = 50):
        super().__init__()
        self.buffer: list[dict[str, Any]] = []
        self.buffer_size = buffer_size
        self._db_path: str | None = None
        self._enabled = True
        self._flush_lock = False
        import threading
        self._lock = threading.Lock()

    def set_database(self, db: Any) -> None:
        """Set the database instance for writing logs."""
        # Get db path from database object
        self._db_path = str(db.db_path)

    def enable(self) -> None:
        """Enable log writing to database."""
        self._enabled = True

    def disable(self) -> None:
        """Disable log writing to database."""
        self._enabled = False

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a log record for writing."""
        if not self._enabled or self._db_path is None:
            return

        try:
            # Format record using our structured formatter
            formatter = StructuredFormatter()
            formatted = formatter.format(record)
            log_data = json.loads(formatted)

            # Add to buffer (thread-safe)
            with self._lock:
                self.buffer.append(log_data)

                # Flush if buffer is full
                if len(self.buffer) >= self.buffer_size:
                    self._flush_in_thread()

        except Exception:
            # Don't let logging errors break the application
            pass

    def _flush_in_thread(self) -> None:
        """Synchronously flush buffered logs to database."""
        if not self.buffer or not self._db_path:
            return

        logs_to_write = list(self.buffer)
        self.buffer = []

        for log_entry in logs_to_write:
            try:
                # Import here to avoid circular dependency
                import sqlite3

                # Create a new event loop if none exists
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError("Event loop is closed")
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                # Try to run async operation
                future = asyncio.create_task(
                    self._write_log(log_entry),
                    loop=loop,
                )
                # Don't wait - let it complete asynchronously
            except Exception:
                # Silently fail - logging shouldn't break the app
                pass

    async def _write_log(self, log_entry: dict[str, Any]) -> None:
        """Write a single log entry to database."""
        if self._db_path is None:
            return

        try:
            await self._db.execute(
                """
                INSERT INTO structured_logs (
                    timestamp, level, logger, message, module, function, line,
                    event_type, project_id, duration_seconds, extra_data, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_entry.get("timestamp"),
                    log_entry.get("level"),
                    log_entry.get("logger"),
                    log_entry.get("message"),
                    log_entry.get("module"),
                    log_entry.get("function"),
                    log_entry.get("line"),
                    log_entry.get("event_type"),
                    log_entry.get("project_id"),
                    log_entry.get("duration_seconds"),
                    json.dumps(log_entry.get("extra_data")) if log_entry.get("extra_data") else None,
                    log_entry.get("correlation_id"),
                ),
            )
        except Exception:
            # Silently fail - logging shouldn't break the app
            pass

    async def flush(self) -> None:
        """Async flush any remaining buffered logs."""
        if self.buffer:
            logs_to_write = list(self.buffer)
            self.buffer = []
            for log_entry in logs_to_write:
                await self._write_log(log_entry)


# Global database handler instance (will be attached after DB init)
_db_handler: DatabaseLogHandler | None = None


def get_database_handler() -> DatabaseLogHandler | None:
    """Get the global database log handler."""
    global _db_handler
    return _db_handler


def attach_database_handler(db: Any) -> DatabaseLogHandler:
    """Create and attach a database log handler to root logger.

    This should be called after database is initialized.
    """
    global _db_handler

    if _db_handler is None:
        _db_handler = DatabaseLogHandler()

    _db_handler.set_database(db)

    # Add to root logger if not already added
    root_logger = logging.getLogger()
    if _db_handler not in root_logger.handlers:
        root_logger.addHandler(_db_handler)


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