"""Supabase PostgREST / RPC adapter for production mode (service_role only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from core.config import AppConfig
from core.errors import ConfigurationError, ExternalProviderError
from core.models import ActorContext, ConsentCategory, FamilyGrant, Role


class SupabaseRpcClient:
    """Thin wrapper over supabase-py calling narrowly scoped server-only RPCs."""

    def __init__(self, config: AppConfig) -> None:
        config.require_production_ready()
        if not config.has_supabase:
            raise ConfigurationError("errors.config_missing", params={"missing": "SUPABASE_URL"})
        try:
            from supabase import create_client

            self._client = create_client(config.supabase_url, config.supabase_service_role_key)
        except Exception as exc:
            raise ConfigurationError(
                "errors.config_missing", params={"missing": "SUPABASE_CLIENT"}
            ) from exc

    def call_rpc(self, fn_name: str, params: dict[str, Any]) -> Any:
        """Invoke a parameterized server-only Postgres RPC without dynamic SQL."""
        try:
            res = self._client.rpc(fn_name, params).execute()
            return res.data
        except Exception as exc:
            raise ExternalProviderError("errors.provider_unavailable") from exc

    def resolve_session(
        self, *, token_hash: str, now: datetime, idle_minutes: int
    ) -> ActorContext | None:
        """Validate session via cc_resolve_session RPC."""
        data = self.call_rpc(
            "cc_resolve_session",
            {
                "p_token_hash": token_hash,
                "p_now": now.isoformat(),
                "p_idle_minutes": idle_minutes,
            },
        )
        if not data:
            return None
        return ActorContext(
            session_id=UUID(data["session_id"]),
            account_id=UUID(data["account_id"]),
            membership_id=UUID(data["membership_id"]),
            circle_id=UUID(data["circle_id"]),
            role=Role(data["role"]),
            display_name=data["display_name"],
            language=data["language"],
            font_px=int(data["font_px"]),
            high_contrast=bool(data["high_contrast"]),
            is_care_contact=bool(data["is_care_contact"]),
            grants=frozenset(FamilyGrant(g) for g in data.get("grants", [])),
            consents=frozenset(ConsentCategory(c) for c in data.get("consents", [])),
            auth_revision=int(data["auth_revision"]),
            circle_timezone=data["circle_timezone"],
            ai_consent_granted=bool(data["ai_consent_granted"]),
        )

    def record_login_attempt(
        self,
        *,
        email_normalized: str,
        success: bool,
        now: datetime,
        max_failures: int,
        lockout_minutes: int,
        global_limit_per_min: int,
    ) -> dict[str, Any]:
        """Atomically record login attempt and lockout via cc_record_login_attempt RPC."""
        return dict(
            self.call_rpc(
                "cc_record_login_attempt",
                {
                    "p_email": email_normalized,
                    "p_success": success,
                    "p_now": now.isoformat(),
                    "p_max_failures": max_failures,
                    "p_lockout_minutes": lockout_minutes,
                    "p_global_limit_per_min": global_limit_per_min,
                },
            )
            or {}
        )

    def confirm_dose_taken(
        self, *, session_token_hash: str, dose_id: UUID, now: datetime
    ) -> dict[str, Any]:
        """Confirm dose taken via cc_confirm_dose_taken transactional RPC."""
        return dict(
            self.call_rpc(
                "cc_confirm_dose_taken",
                {
                    "p_token_hash": session_token_hash,
                    "p_dose_id": str(dose_id),
                    "p_now": now.isoformat(),
                },
            )
            or {}
        )
