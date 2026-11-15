"""Application configuration loading, validation, and secret-free diagnostics."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.errors import ConfigurationError

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _as_int(raw: str | None, default: int, *, minimum: int = 1) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        return default
    return max(val, minimum)


@dataclass(frozen=True)
class AppConfig:
    """Immutable runtime configuration resolved from environment or secrets."""

    demo_mode: bool = False
    environment: str = "production"
    app_url: str = "http://localhost:3000"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    database_url: str = ""
    action_signing_key_id: str = "v1"
    action_signing_key: str = ""
    action_signing_previous_key_id: str = ""
    action_signing_previous_key: str = ""
    action_token_max_ttl_seconds: int = 7200
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    login_global_rate_limit_per_min: int = 60
    bcrypt_rounds: int = 12
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_ssl: bool = False
    smtp_username: str = ""
    smtp_app_password: str = ""
    smtp_from_address: str = ""
    smtp_from_name: str = "CareCircle"
    smtp_timeout_seconds: int = 15
    telegram_bot_token: str = ""
    telegram_timeout_seconds: int = 10
    worker_budget_seconds: int = 45
    worker_batch_size: int = 50
    worker_stale_minutes: int = 20
    adherence_grace_minutes: int = 30

    @property
    def has_supabase(self) -> bool:
        """Return True if Supabase URL and service-role key are configured."""
        return bool(self.supabase_url.strip() and self.supabase_service_role_key.strip())

    @property
    def has_smtp(self) -> bool:
        """Return True if SMTP credentials and sender address are configured."""
        return bool(
            self.smtp_host.strip()
            and self.smtp_username.strip()
            and self.smtp_app_password.strip()
            and self.smtp_from_address.strip()
        )

    @property
    def has_telegram(self) -> bool:
        """Return True if a Telegram bot token is configured."""
        return bool(self.telegram_bot_token.strip())

    @property
    def has_gemini(self) -> bool:
        """Return True if a Gemini API key and model are configured."""
        return bool(self.gemini_api_key.strip() and self.gemini_model.strip())

    def require_production_ready(self) -> None:
        """Fail closed if production mode lacks required database or signing settings."""
        if self.demo_mode:
            return
        missing: list[str] = []
        if not self.has_supabase and not self.database_url.strip():
            missing.append("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY")
        if len(self.action_signing_key.encode("utf-8")) < 32:
            missing.append("ACTION_SIGNING_KEY")
        if missing:
            raise ConfigurationError(
                "errors.config_missing",
                params={"missing": ", ".join(missing)},
                detail="Missing required production configuration",
            )

    def safe_diagnostics(self) -> dict[str, Any]:
        """Return non-secret configuration and capability status for diagnostics."""
        return {
            "mode": "demo" if self.demo_mode else "production",
            "environment": self.environment,
            "database_configured": self.has_supabase or bool(self.database_url.strip()),
            "supabase_configured": self.has_supabase,
            "direct_pg_configured": bool(self.database_url.strip()),
            "action_signing_configured": len(self.action_signing_key.encode("utf-8")) >= 32,
            "action_signing_rotation_active": bool(
                self.action_signing_previous_key_id and self.action_signing_previous_key
            ),
            "smtp_configured": self.has_smtp,
            "telegram_configured": self.has_telegram,
            "gemini_configured": self.has_gemini,
            "gemini_model": self.gemini_model if self.has_gemini else "disabled",
            "bcrypt_rounds": self.bcrypt_rounds,
            "session_idle_minutes": self.session_idle_minutes,
            "session_absolute_hours": self.session_absolute_hours,
        }


def load_dotenv_dev_only() -> None:
    """Load local .env only when running in development mode."""
    env_name = os.environ.get("CARECIRCLE_ENV", "development").strip().lower()
    if env_name in {"development", "dev", "local", "test"}:
        try:
            from dotenv import load_dotenv

            load_dotenv(override=False)
        except ImportError:
            pass


def load_config(
    overrides: Mapping[str, str] | None = None,
    *,
    load_dev_env: bool = True,
) -> AppConfig:
    """Load AppConfig from environment variables and optional explicit overrides."""
    if load_dev_env and overrides is None:
        load_dotenv_dev_only()

    src: dict[str, str] = dict(os.environ)
    if overrides:
        src.update({k: str(v) for k, v in overrides.items()})

    demo_mode = _as_bool(src.get("CARECIRCLE_DEMO_MODE"), default=False)
    signing_key = src.get("ACTION_SIGNING_KEY", "")
    if demo_mode and not signing_key:
        # Deterministic isolated signing key used exclusively in demo mode
        signing_key = "carecircle-isolated-demo-signing-key-32bytes-min"

    bcrypt_rounds = _as_int(src.get("BCRYPT_ROUNDS"), default=12, minimum=4)
    if not demo_mode and bcrypt_rounds < 12:
        bcrypt_rounds = 12

    app_url = src.get("APP_URL", "http://localhost:3000").strip().rstrip("/")
    if not app_url:
        app_url = "http://localhost:3000"

    return AppConfig(
        demo_mode=demo_mode,
        environment=src.get("CARECIRCLE_ENV", "production").strip() or "production",
        app_url=app_url,
        supabase_url=src.get("SUPABASE_URL", "").strip(),
        supabase_service_role_key=src.get("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        database_url=src.get("DATABASE_URL", "").strip(),
        action_signing_key_id=src.get("ACTION_SIGNING_KEY_ID", "v1").strip() or "v1",
        action_signing_key=signing_key,
        action_signing_previous_key_id=src.get("ACTION_SIGNING_PREVIOUS_KEY_ID", "").strip(),
        action_signing_previous_key=src.get("ACTION_SIGNING_PREVIOUS_KEY", ""),
        action_token_max_ttl_seconds=_as_int(
            src.get("ACTION_TOKEN_MAX_TTL_SECONDS"), default=7200, minimum=60
        ),
        session_idle_minutes=_as_int(src.get("SESSION_IDLE_MINUTES"), default=30, minimum=1),
        session_absolute_hours=_as_int(src.get("SESSION_ABSOLUTE_HOURS"), default=12, minimum=1),
        login_max_failures=_as_int(src.get("LOGIN_MAX_FAILURES"), default=5, minimum=1),
        login_lockout_minutes=_as_int(src.get("LOGIN_LOCKOUT_MINUTES"), default=15, minimum=1),
        login_global_rate_limit_per_min=_as_int(
            src.get("LOGIN_GLOBAL_RATE_LIMIT_PER_MIN"), default=60, minimum=1
        ),
        bcrypt_rounds=bcrypt_rounds,
        gemini_api_key=src.get("GEMINI_API_KEY", "").strip(),
        gemini_model=src.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        smtp_host=src.get("SMTP_HOST", "smtp.gmail.com").strip(),
        smtp_port=_as_int(src.get("SMTP_PORT"), default=587, minimum=1),
        smtp_use_ssl=_as_bool(src.get("SMTP_USE_SSL"), default=False),
        smtp_username=src.get("SMTP_USERNAME", "").strip(),
        smtp_app_password=src.get("SMTP_APP_PASSWORD", ""),
        smtp_from_address=src.get("SMTP_FROM_ADDRESS", "").strip(),
        smtp_from_name=src.get("SMTP_FROM_NAME", "CareCircle").strip() or "CareCircle",
        smtp_timeout_seconds=_as_int(src.get("SMTP_TIMEOUT_SECONDS"), default=15, minimum=1),
        telegram_bot_token=src.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_timeout_seconds=_as_int(
            src.get("TELEGRAM_TIMEOUT_SECONDS"), default=10, minimum=1
        ),
        worker_budget_seconds=_as_int(src.get("WORKER_BUDGET_SECONDS"), default=45, minimum=5),
        worker_batch_size=_as_int(src.get("WORKER_BATCH_SIZE"), default=50, minimum=1),
        worker_stale_minutes=_as_int(src.get("WORKER_STALE_MINUTES"), default=20, minimum=1),
        adherence_grace_minutes=_as_int(src.get("ADHERENCE_GRACE_MINUTES"), default=30, minimum=1),
    )
