"""Structured JSON logging with strict redaction of PHI, secrets, tokens, and emails."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passcode",
        "passcode_hash",
        "token",
        "raw_token",
        "session_token",
        "session_id_raw",
        "secret",
        "api_key",
        "service_role_key",
        "app_password",
        "email",
        "to_email",
        "from_email",
        "prompt",
        "response_body",
        "medical_note",
        "notes",
        "instructions",
        "transcript",
        "image_bytes",
        "html",
        "url",
        "link",
    }
)

_TOKEN_QUERY_RE = re.compile(
    r"([?&](?:action_token|token|verify|invite|code|secret|sig)=)([^&\s]+)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def new_correlation_id() -> str:
    """Return a fresh opaque correlation identifier."""
    return uuid.uuid4().hex[:16]


def redact_text(value: str) -> str:
    """Strip query tokens and email addresses from arbitrary log strings."""
    cleaned = _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", value)
    cleaned = _EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    return cleaned


def sanitize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of structured log fields with sensitive keys and tokens redacted."""
    sanitized: dict[str, Any] = {}
    for key, val in fields.items():
        lowered = key.lower()
        if lowered in _SENSITIVE_KEYS or any(
            part in lowered for part in ("secret", "passcode", "password", "token", "email", "note")
        ):
            sanitized[key] = "[REDACTED]"
        elif isinstance(val, str):
            sanitized[key] = redact_text(val)
        elif isinstance(val, dict):
            sanitized[key] = sanitize_log_fields(val)
        else:
            sanitized[key] = val
    return sanitized


class StructuredLogger:
    """Emit structured JSON log records containing event codes and safe metadata only."""

    def __init__(self, name: str = "carecircle") -> None:
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def log(
        self,
        level: int,
        event: str,
        *,
        correlation_id: str | None = None,
        **fields: Any,
    ) -> None:
        """Emit a sanitized JSON log entry."""
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": logging.getLevelName(level),
            "event": event,
            "correlation_id": correlation_id or new_correlation_id(),
            **sanitize_log_fields(fields),
        }
        self._logger.log(level, json.dumps(payload, default=str, ensure_ascii=True))

    def info(self, event: str, *, correlation_id: str | None = None, **fields: Any) -> None:
        """Emit an INFO structured event."""
        self.log(logging.INFO, event, correlation_id=correlation_id, **fields)

    def warning(self, event: str, *, correlation_id: str | None = None, **fields: Any) -> None:
        """Emit a WARNING structured event."""
        self.log(logging.WARNING, event, correlation_id=correlation_id, **fields)

    def error(self, event: str, *, correlation_id: str | None = None, **fields: Any) -> None:
        """Emit an ERROR structured event."""
        self.log(logging.ERROR, event, correlation_id=correlation_id, **fields)


def get_logger(name: str = "carecircle") -> StructuredLogger:
    """Return a structured logger instance."""
    return StructuredLogger(name)
