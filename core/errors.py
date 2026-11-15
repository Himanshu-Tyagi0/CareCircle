"""Explicit domain exceptions carrying locale keys and safe parameters."""

from __future__ import annotations

from typing import Any


class CareCircleError(Exception):
    """Base domain error with localized message key and safe parameters."""

    code: str = "internal_error"
    locale_key: str = "errors.internal"
    http_status: int = 400

    def __init__(
        self,
        locale_key: str | None = None,
        *,
        code: str | None = None,
        params: dict[str, Any] | None = None,
        detail: str = "",
    ) -> None:
        if locale_key:
            self.locale_key = locale_key
        if code:
            self.code = code
        self.params: dict[str, Any] = dict(params or {})
        self.detail = detail
        super().__init__(detail or self.locale_key)


class ValidationError(CareCircleError):
    """Input validation failed."""

    code = "validation_error"
    locale_key = "errors.validation"
    http_status = 422


class AuthenticationError(CareCircleError):
    """Authentication failed (generic to prevent enumeration)."""

    code = "authentication_failed"
    locale_key = "errors.auth_invalid"
    http_status = 401


class AccountLockedError(CareCircleError):
    """Account temporarily locked due to consecutive failed login attempts."""

    code = "account_locked"
    locale_key = "errors.account_locked"
    http_status = 429


class RateLimitError(CareCircleError):
    """Bounded global or action rate limit exceeded."""

    code = "rate_limited"
    locale_key = "errors.rate_limited"
    http_status = 429


class AuthorizationError(CareCircleError):
    """Actor lacks role or explicit grant for requested action."""

    code = "forbidden"
    locale_key = "errors.forbidden"
    http_status = 403


class ConsentRequiredError(CareCircleError):
    """Elder has not granted or has revoked category/AI consent."""

    code = "consent_required"
    locale_key = "errors.consent_required"
    http_status = 403


class NotFoundError(CareCircleError):
    """Requested resource does not exist or is outside actor's circle."""

    code = "not_found"
    locale_key = "errors.not_found"
    http_status = 404


class ConflictError(CareCircleError):
    """Concurrency conflict, duplicate unique key, or invalid state transition."""

    code = "conflict"
    locale_key = "errors.conflict"
    http_status = 409


class TokenInvalidError(CareCircleError):
    """Signed or opaque verification/invitation/action token is invalid or expired."""

    code = "token_invalid"
    locale_key = "errors.token_invalid"
    http_status = 400


class ConfigurationError(CareCircleError):
    """Required runtime configuration or credentials are missing."""

    code = "configuration_error"
    locale_key = "errors.config_missing"
    http_status = 503


class ExternalProviderError(CareCircleError):
    """External provider call failed safely without leaking raw provider response."""

    code = "provider_error"
    locale_key = "errors.provider_unavailable"
    http_status = 502


class AIServiceError(CareCircleError):
    """Bounded AI processing failed, timed out, or returned invalid/unsafe output."""

    code = "ai_error"
    locale_key = "errors.ai_unavailable"
    http_status = 502
