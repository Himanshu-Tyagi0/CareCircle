"""Authentication, verified elder onboarding, lockout, and session management service."""

from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.clock import Clock
from core.config import AppConfig
from core.errors import (
    AccountLockedError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    RateLimitError,
    TokenInvalidError,
    ValidationError,
)
from core.i18n import SUPPORTED_LANGUAGES, t
from core.logging import get_logger
from core.models import Account, AccountStatus, ActorContext, CareCircle, Membership
from core.permissions import Action, authorize
from core.security import (
    dummy_verify_passcode,
    escape_html,
    generate_opaque_token,
    hash_passcode,
    hash_token,
    normalize_email,
    verify_passcode,
)
from integrations.email_gateway import DeliveryState, EmailGateway, EmailPayload
from repositories.interfaces import AuthRepository

_LOG = get_logger("carecircle.auth")


def validate_timezone(tz_name: str) -> str:
    """Validate an IANA timezone string using zoneinfo."""
    cleaned = tz_name.strip()
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ValidationError("errors.validation") from exc
    return cleaned


class AuthService:
    """Trusted server-side authentication, verified onboarding, and session service."""

    def __init__(
        self,
        *,
        auth_repo: AuthRepository,
        email_gateway: EmailGateway,
        config: AppConfig,
        clock: Clock,
    ) -> None:
        self._repo = auth_repo
        self._email = email_gateway
        self._cfg = config
        self._clock = clock

    def start_elder_onboarding(
        self,
        *,
        email: str,
        display_name: str,
        passcode: str,
        timezone: str,
        language: str = "en",
    ) -> str | None:
        """Create pending verification and send verification link (raw token returned ONLY in demo mode)."""
        norm_email = normalize_email(email)
        name = display_name.strip()
        if not name or len(name) > 120:
            raise ValidationError("errors.validation")
        tz = validate_timezone(timezone)
        lang = language if language in SUPPORTED_LANGUAGES else "en"
        pass_hash = hash_passcode(passcode, rounds=self._cfg.bcrypt_rounds)

        if not self._cfg.demo_mode and not self._cfg.has_smtp:
            raise ConfigurationError("errors.config_missing", params={"missing": "SMTP"})

        now = self._clock.now()
        raw_token = generate_opaque_token(32)
        tok_hash = hash_token(raw_token)
        expires_at = now + timedelta(hours=2)

        self._repo.create_pending_verification(
            token_hash=tok_hash,
            email_normalized=norm_email,
            display_name=name,
            passcode_hash=pass_hash,
            timezone=tz,
            language=lang,
            expires_at=expires_at,
            now=now,
        )

        link = f"{self._cfg.app_url}/?verify={raw_token}"
        subject = t("notifications.verify_subject", lang=lang)
        body = t("notifications.verify_body", lang=lang, link=link)
        html_body = (
            f"<p>{escape_html(t('notifications.verify_subject', lang=lang))}</p>"
            f"<p><a href='{escape_html(link)}'>{escape_html(t('auth.verification_confirm_button', lang=lang))}</a></p>"
        )
        res = self._email.send(
            EmailPayload(
                to_address=norm_email,
                subject=subject,
                text_body=body,
                html_body=html_body,
                message_id=f"<verify-{uuid.uuid4()}@carecircle.local>",
            )
        )
        _LOG.info("onboarding_verification_prepared", delivery_state=res.state.value)
        if res.state == DeliveryState.DEFINITIVE_FAILURE and not self._cfg.demo_mode:
            raise ConfigurationError("errors.provider_unavailable")
        return raw_token if self._cfg.demo_mode else None

    def preview_verification(self, raw_token: str) -> dict[str, str]:
        """Harmless GET preview for email verification link; never mutates state."""
        if not raw_token or len(raw_token) > 512:
            raise TokenInvalidError("errors.token_invalid")
        pv = self._repo.get_pending_verification_by_hash(hash_token(raw_token))
        now = self._clock.now()
        if not pv or pv.consumed_at is not None:
            raise TokenInvalidError("errors.token_invalid")
        if pv.expires_at <= now:
            raise TokenInvalidError("errors.token_expired")
        return {
            "display_name": pv.display_name,
            "timezone": pv.timezone,
            "language": pv.language,
        }

    def confirm_verification(self, raw_token: str) -> dict[str, object]:
        """Atomically redeem verification token to activate account, circle, and elder role."""
        if not raw_token or len(raw_token) > 512:
            raise TokenInvalidError("errors.token_invalid")
        res = self._repo.redeem_verification(
            token_hash=hash_token(raw_token), now=self._clock.now()
        )
        status = res.get("status")
        if status == "conflict":
            raise ConflictError("errors.conflict")
        if status != "ok":
            raise TokenInvalidError("errors.token_invalid")
        _LOG.info("elder_onboarding_completed", circle_id=str(res.get("circle_id", "")))
        return res

    def authenticate(
        self, *, email: str, passcode: str
    ) -> tuple[Account, list[tuple[Membership, CareCircle]]]:
        """Verify email and passcode with atomic lockout counters and generic failures."""
        norm_email = normalize_email(email)
        now = self._clock.now()
        acc = self._repo.get_account_by_email(norm_email)

        if acc is None:
            dummy_verify_passcode(passcode)
            res = self._repo.record_login_attempt(
                email_normalized=norm_email,
                success=False,
                now=now,
                max_failures=self._cfg.login_max_failures,
                lockout_minutes=self._cfg.login_lockout_minutes,
                global_limit_per_min=self._cfg.login_global_rate_limit_per_min,
            )
            if res.get("status") == "rate_limited":
                raise RateLimitError("errors.rate_limited")
            raise AuthenticationError("errors.auth_invalid")

        if acc.status != AccountStatus.ACTIVE:
            dummy_verify_passcode(passcode)
            raise AuthenticationError("errors.auth_invalid")

        if acc.locked_until is not None and acc.locked_until > now:
            raise AccountLockedError(
                "errors.account_locked", params={"minutes": self._cfg.login_lockout_minutes}
            )

        ok = verify_passcode(passcode, acc.passcode_hash)
        attempt_res = self._repo.record_login_attempt(
            email_normalized=norm_email,
            success=ok,
            now=now,
            max_failures=self._cfg.login_max_failures,
            lockout_minutes=self._cfg.login_lockout_minutes,
            global_limit_per_min=self._cfg.login_global_rate_limit_per_min,
        )
        status = attempt_res.get("status")
        if status == "rate_limited":
            raise RateLimitError("errors.rate_limited")
        if status == "locked":
            raise AccountLockedError(
                "errors.account_locked", params={"minutes": self._cfg.login_lockout_minutes}
            )
        if not ok or status != "ok":
            _LOG.warning("login_failed", account_id=str(acc.id))
            raise AuthenticationError("errors.auth_invalid")

        memberships = self._repo.list_memberships_for_account(acc.id)
        if not memberships:
            raise AuthenticationError("errors.auth_invalid")
        refreshed = self._repo.get_account_by_email(norm_email) or acc
        _LOG.info("login_succeeded", account_id=str(acc.id), membership_count=len(memberships))
        return refreshed, memberships

    def issue_session(
        self, *, account_id: uuid.UUID, membership_id: uuid.UUID
    ) -> tuple[str, ActorContext]:
        """Issue an opaque session bound to one verified membership."""
        raw_token = generate_opaque_token(32)
        actor = self._repo.create_session(
            token_hash=hash_token(raw_token),
            account_id=account_id,
            membership_id=membership_id,
            now=self._clock.now(),
            idle_minutes=self._cfg.session_idle_minutes,
            absolute_hours=self._cfg.session_absolute_hours,
        )
        _LOG.info("session_issued", account_id=str(account_id), circle_id=str(actor.circle_id))
        return raw_token, actor

    def resolve_session(self, raw_session_token: str | None) -> ActorContext | None:
        """Revalidate session expiry, status, membership, and auth revision."""
        if not raw_session_token:
            return None
        return self._repo.resolve_session(
            token_hash=hash_token(raw_session_token),
            now=self._clock.now(),
            idle_minutes=self._cfg.session_idle_minutes,
        )

    def logout(self, raw_session_token: str | None) -> None:
        """Revoke the current session token."""
        if raw_session_token:
            self._repo.revoke_session(
                token_hash=hash_token(raw_session_token), now=self._clock.now()
            )
            _LOG.info("session_revoked")

    def update_own_preferences(
        self,
        actor: ActorContext,
        *,
        language: str,
        font_px: int,
        high_contrast: bool,
    ) -> Account:
        """Persist member's own language, validated text size (18..32), and contrast mode."""
        authorize(actor, Action.UPDATE_OWN_PREFERENCES)
        if language not in SUPPORTED_LANGUAGES:
            raise ValidationError("errors.validation")
        if isinstance(font_px, bool) or not isinstance(font_px, int) or not (18 <= font_px <= 32):
            raise ValidationError("errors.validation")
        return self._repo.update_preferences(
            account_id=actor.account_id,
            language=language,
            font_px=font_px,
            high_contrast=bool(high_contrast),
        )

    def update_own_telegram(
        self, actor: ActorContext, *, opt_in: bool, chat_id: str | None
    ) -> Account:
        """Allow an active member to opt in/out and bind their own Telegram chat ID."""
        authorize(actor, Action.UPDATE_OWN_PREFERENCES)
        cleaned_chat = (chat_id or "").strip() or None
        if opt_in and not cleaned_chat:
            raise ValidationError("errors.validation")
        return self._repo.update_telegram_binding(
            account_id=actor.account_id, opt_in=opt_in, chat_id=cleaned_chat
        )

    def change_own_passcode(
        self,
        actor: ActorContext,
        *,
        current_passcode: str,
        new_passcode: str,
        current_raw_session_token: str | None = None,
    ) -> None:
        """Verify current passcode, update hash, and revoke other active sessions."""
        authorize(actor, Action.CHANGE_OWN_PASSCODE)
        acc = self._repo.get_account_by_id(actor.account_id)
        if not acc or not verify_passcode(current_passcode, acc.passcode_hash):
            raise AuthenticationError("errors.auth_invalid")
        new_hash = hash_passcode(new_passcode, rounds=self._cfg.bcrypt_rounds)
        keep_hash = hash_token(current_raw_session_token) if current_raw_session_token else None
        self._repo.update_passcode_and_revoke(
            account_id=actor.account_id,
            new_passcode_hash=new_hash,
            now=self._clock.now(),
            keep_session_hash=keep_hash,
        )
        _LOG.info("passcode_changed_sessions_revoked", account_id=str(actor.account_id))

    def create_own_circle(
        self, actor: ActorContext, *, display_name: str, timezone: str
    ) -> tuple[CareCircle, Membership]:
        """Allow an authenticated account without an owned circle to create their own."""
        authorize(actor, Action.CREATE_OWN_CIRCLE)
        name = display_name.strip()
        if not name or len(name) > 120:
            raise ValidationError("errors.validation")
        tz = validate_timezone(timezone)
        return self._repo.create_own_circle(
            account_id=actor.account_id,
            display_name=name,
            timezone=tz,
            now=self._clock.now(),
        )
