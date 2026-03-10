"""Service-level exceptions exposed to the API layer."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for service errors."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    """Raised when an entity is not present."""

    status_code = 404


class ConflictError(ServiceError):
    """Raised when an operation would violate service state."""

    status_code = 409
