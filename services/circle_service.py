"""Care Circle governance: invitations, consent matrix, family grants, policies, and audit."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from typing import Any
from uuid import UUID

from core.clock import Clock
from core.config import AppConfig
from core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TokenInvalidError,
    ValidationError,
)
from core.i18n import t
from core.logging import get_logger
from core.models import (
    Account,
    ActorContext,
    CareCircle,
    ConsentCategory,
    FamilyGrant,
    MemberConsent,
    Membership,
    Role,
)
from core.models_clinical import AuditEvent
from core.permissions import Action, authorize
from core.security import (
    escape_html,
    generate_opaque_token,
    hash_passcode,
    hash_token,
    normalize_email,
    verify_passcode,
)
from integrations.email_gateway import EmailGateway, EmailPayload
from repositories.interfaces import (
    AuthRepository,
    CircleRepository,
    CollabRepository,
    OutboxRepository,
)

from services.auth_service import validate_timezone

_LOG = get_logger("carecircle.circle")

EMERGENCY_NUMBERS_BY_COUNTRY: dict[str, str] = {
    "IN": "112",
    "US": "911",
    "CA": "911",
    "GB": "999 / 112",
    "AU": "000",
    "EU": "112",
    "SG": "995",
}


class CircleService:
    """Elder-controlled care circle membership, consent, policy, and audit operations."""

    def __init__(
        self,
        *,
        auth_repo: AuthRepository,
        circle_repo: CircleRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        email_gateway: EmailGateway,
        config: AppConfig,
        clock: Clock,
    ) -> None:
        self._auth = auth_repo
        self._circle = circle_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._email = email_gateway
        self._cfg = config
        self._clock = clock

    def get_circle(self, actor: ActorContext) -> CareCircle:
        """Return the actor's active Care Circle."""
        circle = self._circle.get_circle(actor.circle_id)
        if not circle:
            raise NotFoundError("errors.not_found")
        return circle

    def create_invitation(
        self,
        actor: ActorContext,
        *,
        invited_email: str,
        role: Role,
        grants: frozenset[FamilyGrant] = frozenset(),
        initial_consents: frozenset[ConsentCategory] = frozenset(),
        is_care_contact: bool = False,
    ) -> str:
        """Create an email-bound single-use invitation (elder only) and send invite link."""
        authorize(actor, Action.INVITE_MEMBER)
        if role not in (Role.CARETAKER, Role.FAMILY):
            raise ValidationError("errors.validation")
        norm_email = normalize_email(invited_email)
        now = self._clock.now()
        raw_token = generate_opaque_token(32)
        self._auth.create_invitation(
            circle_id=actor.circle_id,
            token_hash=hash_token(raw_token),
            invited_email_normalized=norm_email,
            role=role,
            grants=grants if role == Role.FAMILY else frozenset(),
            initial_consents=initial_consents,
            is_care_contact=is_care_contact,
            invited_by_account_id=actor.account_id,
            expires_at=now + timedelta(hours=72),
            now=now,
        )
        link = f"{self._cfg.app_url}/?invite={raw_token}"
        subject = t("notifications.invite_subject", lang=actor.language)
        body = t("notifications.invite_body", lang=actor.language, link=link)
        html_body = (
            f"<p>{escape_html(subject)}</p>"
            f"<p><a href='{escape_html(link)}'>{escape_html(t('auth.invite_confirm_button', lang=actor.language))}</a></p>"
        )
        self._email.send(
            EmailPayload(
                to_address=norm_email,
                subject=subject,
                text_body=body,
                html_body=html_body,
                message_id=f"<invite-{uuid.uuid4()}@carecircle.local>",
            )
        )
        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=actor.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="invitation_created",
                target_type="invitation",
                target_id=role.value,
                metadata={"initial_consents": sorted(c.value for c in initial_consents)},
                occurred_at=now,
            )
        )
        _LOG.info("invitation_created", circle_id=str(actor.circle_id), role=role.value)
        return raw_token

    def preview_invitation(self, raw_token: str) -> dict[str, Any]:
        """Harmless GET preview for an invitation link; never mutates state."""
        if not raw_token or len(raw_token) > 512:
            raise TokenInvalidError("errors.token_invalid")
        found = self._auth.get_invitation_by_hash(hash_token(raw_token))
        if not found:
            raise TokenInvalidError("errors.token_invalid")
        inv, circle = found
        now = self._clock.now()
        if inv.consumed_at is not None or inv.revoked_at is not None:
            raise TokenInvalidError("errors.token_invalid")
        if inv.expires_at <= now:
            raise TokenInvalidError("errors.token_expired")
        existing = self._auth.get_account_by_email(inv.invited_email_normalized)
        return {
            "circle_name": circle.display_name,
            "role": inv.role,
            "invited_email": inv.invited_email_normalized,
            "existing_account": existing is not None,
            "initial_consents": inv.initial_consents,
        }

    def accept_invitation(
        self,
        *,
        raw_token: str,
        passcode: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Accept a single-use email-bound invitation without replacing existing passcodes."""
        found = self._auth.get_invitation_by_hash(hash_token(raw_token))
        if not found:
            raise TokenInvalidError("errors.token_invalid")
        inv, _ = found
        now = self._clock.now()
        if inv.consumed_at is not None or inv.revoked_at is not None:
            raise TokenInvalidError("errors.token_invalid")
        if inv.expires_at <= now:
            raise TokenInvalidError("errors.token_expired")

        existing = self._auth.get_account_by_email(inv.invited_email_normalized)
        if existing is not None:
            if not verify_passcode(passcode, existing.passcode_hash):
                raise AuthenticationError("errors.auth_invalid")
            res = self._auth.redeem_invitation(
                token_hash=hash_token(raw_token),
                now=now,
                passcode_hash=None,
                display_name=None,
                existing_account_id=existing.id,
            )
        else:
            name = (display_name or "").strip()
            if not name or len(name) > 120:
                raise ValidationError("errors.validation")
            pass_hash = hash_passcode(passcode, rounds=self._cfg.bcrypt_rounds)
            res = self._auth.redeem_invitation(
                token_hash=hash_token(raw_token),
                now=now,
                passcode_hash=pass_hash,
                display_name=name,
                existing_account_id=None,
            )

        status = res.get("status")
        if status == "conflict":
            raise ConflictError("errors.conflict")
        if status != "ok":
            raise TokenInvalidError("errors.token_invalid")
        _LOG.info("invitation_redeemed", circle_id=str(inv.circle_id))
        return res

    def list_members(
        self, actor: ActorContext
    ) -> list[tuple[Membership, Account, list[MemberConsent]]]:
        """List circle members; full consent details for elder, active list for members."""
        members = self._circle.list_members(actor.circle_id)
        if actor.role == Role.ELDER:
            return members
        return [
            (mem, acc.model_copy(update={"email_normalized": "***", "passcode_hash": "***"}), cons)
            for mem, acc, cons in members
            if mem.active
        ]

    def update_member_consent(
        self,
        actor: ActorContext,
        *,
        membership_id: UUID,
        category: ConsentCategory,
        granted: bool,
    ) -> None:
        """Grant or revoke a member's category consent and invalidate dependent state."""
        authorize(actor, Action.MANAGE_CONSENT, target_circle_id=actor.circle_id)
        now = self._clock.now()
        self._circle.update_member_consent(
            circle_id=actor.circle_id,
            membership_id=membership_id,
            category=category,
            granted=granted,
            actor_account_id=actor.account_id,
            now=now,
        )
        if not granted:
            self._outbox.cancel_outbox_for_recipient(
                circle_id=actor.circle_id,
                recipient_membership_id=membership_id,
                category=category,
                now=now,
            )
            self._collab.invalidate_brief_approvals(circle_id=actor.circle_id)
        _LOG.info(
            "consent_changed",
            circle_id=str(actor.circle_id),
            category=category.value,
            granted=granted,
        )

    def update_member_grants(
        self,
        actor: ActorContext,
        *,
        membership_id: UUID,
        grants: frozenset[FamilyGrant],
        is_care_contact: bool,
    ) -> Membership:
        """Update specific family edit grants and care-contact acknowledgement capability."""
        authorize(actor, Action.MANAGE_FAMILY_GRANTS, target_circle_id=actor.circle_id)
        mem = self._circle.get_membership(actor.circle_id, membership_id)
        if not mem or mem.role == Role.ELDER:
            raise NotFoundError("errors.not_found")
        effective_grants = grants if mem.role == Role.FAMILY else frozenset()
        return self._circle.update_member_grants(
            circle_id=actor.circle_id,
            membership_id=membership_id,
            grants=effective_grants,
            is_care_contact=is_care_contact,
            actor_account_id=actor.account_id,
            now=self._clock.now(),
        )

    def remove_member(self, actor: ActorContext, *, membership_id: UUID) -> None:
        """Deactivate a member, revoke their sessions, and invalidate brief approvals."""
        authorize(actor, Action.MANAGE_MEMBERS, target_circle_id=actor.circle_id)
        if membership_id == actor.membership_id:
            raise AuthorizationError("errors.forbidden")
        now = self._clock.now()
        self._circle.deactivate_member(
            circle_id=actor.circle_id,
            membership_id=membership_id,
            actor_account_id=actor.account_id,
            now=now,
        )
        self._outbox.cancel_outbox_for_recipient(
            circle_id=actor.circle_id,
            recipient_membership_id=membership_id,
            category=None,
            now=now,
        )
        self._collab.invalidate_brief_approvals(circle_id=actor.circle_id)

    def update_policies(
        self,
        actor: ActorContext,
        *,
        timezone: str | None = None,
        quiet_hours_enabled: bool | None = None,
        quiet_hours_start: time | None = None,
        quiet_hours_end: time | None = None,
        elder_quiet_reminders_enabled: bool | None = None,
        checkin_enabled: bool | None = None,
        checkin_time: time | None = None,
        checkin_window_minutes: int | None = None,
        primary_contact_membership_id: UUID | None = None,
        secondary_contact_membership_id: UUID | None = None,
        checkin_fallback_membership_id: UUID | None = None,
        clear_secondary_contact: bool = False,
        clear_checkin_fallback: bool = False,
        emergency_country_code: str | None = None,
        emergency_contact_name: str | None = None,
        emergency_contact_phone: str | None = None,
        ai_consent_granted: bool | None = None,
    ) -> CareCircle:
        """Update elder-controlled circle policies, contacts, and optional AI consent."""
        authorize(actor, Action.MANAGE_CONTACT_POLICY, target_circle_id=actor.circle_id)
        updates: dict[str, Any] = {}
        now = self._clock.now()
        if timezone is not None:
            updates["timezone"] = validate_timezone(timezone)
        if quiet_hours_enabled is not None:
            updates["quiet_hours_enabled"] = quiet_hours_enabled
        if quiet_hours_start is not None:
            updates["quiet_hours_start"] = quiet_hours_start
        if quiet_hours_end is not None:
            updates["quiet_hours_end"] = quiet_hours_end
        if elder_quiet_reminders_enabled is not None:
            updates["elder_quiet_reminders_enabled"] = elder_quiet_reminders_enabled
        if checkin_enabled is not None:
            updates["checkin_enabled"] = checkin_enabled
            if checkin_enabled:
                existing = self.get_circle(actor)
                if existing.checkin_enabled_from is None:
                    updates["checkin_enabled_from"] = now
        if checkin_time is not None:
            updates["checkin_time"] = checkin_time
        if checkin_window_minutes is not None:
            if not (15 <= checkin_window_minutes <= 720):
                raise ValidationError("errors.validation")
            updates["checkin_window_minutes"] = checkin_window_minutes

        for field, val in (
            ("primary_contact_membership_id", primary_contact_membership_id),
            ("secondary_contact_membership_id", secondary_contact_membership_id),
            ("checkin_fallback_membership_id", checkin_fallback_membership_id),
        ):
            if val is not None:
                mem = self._circle.get_membership(actor.circle_id, val)
                if not mem or not mem.active or mem.role == Role.ELDER:
                    raise AuthorizationError("errors.cross_circle_denied")
                updates[field] = val
        if clear_secondary_contact:
            updates["secondary_contact_membership_id"] = None
        if clear_checkin_fallback:
            updates["checkin_fallback_membership_id"] = None

        if emergency_country_code is not None:
            code = emergency_country_code.strip().upper()
            if code and code not in EMERGENCY_NUMBERS_BY_COUNTRY:
                raise ValidationError("errors.validation")
            updates["emergency_country_code"] = code or None
        if emergency_contact_name is not None:
            updates["emergency_contact_name"] = emergency_contact_name.strip()[:120] or None
        if emergency_contact_phone is not None:
            updates["emergency_contact_phone"] = emergency_contact_phone.strip()[:40] or None
        if ai_consent_granted is not None:
            authorize(actor, Action.MANAGE_AI_CONSENT, target_circle_id=actor.circle_id)
            updates["ai_consent_granted"] = ai_consent_granted
            updates["ai_consent_updated_at"] = now

        return self._circle.update_circle_policies(
            circle_id=actor.circle_id, updates=updates, actor_account_id=actor.account_id, now=now
        )

    def list_audit_log(self, actor: ActorContext, *, limit: int = 100) -> list[AuditEvent]:
        """Return circle audit trail for elder review."""
        authorize(actor, Action.VIEW_AUDIT_LOG, target_circle_id=actor.circle_id)
        return self._outbox.list_audit(actor.circle_id, limit=min(limit, 200))

    def delete_circle(self, actor: ActorContext, *, confirmation_text: str) -> None:
        """Delete and purge an elder's Care Circle after explicit confirmation."""
        authorize(actor, Action.DELETE_CIRCLE, target_circle_id=actor.circle_id)
        if confirmation_text.strip() != "DELETE":
            raise ValidationError("errors.validation")
        self._circle.delete_circle_purge(
            circle_id=actor.circle_id, actor_account_id=actor.account_id, now=self._clock.now()
        )
        _LOG.info("circle_deleted", circle_id=str(actor.circle_id))
