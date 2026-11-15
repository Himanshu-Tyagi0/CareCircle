"""HMAC-SHA256 signed, single-use, expiring email action token issuance and redemption."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.clock import Clock
from core.config import AppConfig
from core.errors import (
    AuthorizationError,
    NotFoundError,
    TokenInvalidError,
    ValidationError,
)
from core.logging import get_logger
from core.models import (
    ActionTokenType,
    ActorContext,
    CantTakeReason,
    CheckInMood,
    ConsentCategory,
    DoseState,
)
from core.models_clinical import ActionTokenRecord, AuditEvent
from core.permissions import Action, authorize
from core.security import hash_token, sign_action_token, verify_action_token
from repositories.interfaces import (
    AuthRepository,
    CircleRepository,
    CollabRepository,
    DoseRepository,
    OutboxRepository,
)

_LOG = get_logger("carecircle.tokens")


@dataclass(frozen=True)
class ActionTokenPreview:
    """Safe preview metadata shown on harmless GET confirmation screen."""

    action: ActionTokenType
    resource_type: str
    resource_id: UUID
    circle_id: UUID
    recipient_membership_id: UUID
    elder_name: str
    context: dict[str, Any]


class ActionTokenService:
    """Issues and atomically redeems single-use signed action tokens."""

    def __init__(
        self,
        *,
        auth_repo: AuthRepository,
        circle_repo: CircleRepository,
        dose_repo: DoseRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        config: AppConfig,
        clock: Clock,
    ) -> None:
        self._auth = auth_repo
        self._circle = circle_repo
        self._dose = dose_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._cfg = config
        self._clock = clock

    def issue_token_in_memory(
        self,
        *,
        action: ActionTokenType,
        circle_id: UUID,
        resource_type: str,
        resource_id: UUID,
        recipient_membership_id: UUID,
        window_ends_at: datetime | None = None,
    ) -> str:
        """Create a signed token immediately before send; store ONLY its SHA-256 hash."""
        now = self._clock.now()
        default_exp = now + timedelta(seconds=self._cfg.action_token_max_ttl_seconds)
        expires_at = min(default_exp, window_ends_at) if window_ends_at else default_exp
        if expires_at <= now:
            expires_at = now + timedelta(minutes=5)

        raw_token, tok_hash = sign_action_token(
            key_id=self._cfg.action_signing_key_id,
            signing_key=self._cfg.action_signing_key,
            action=action,
            circle_id=circle_id,
            resource_type=resource_type,
            resource_id=resource_id,
            recipient_membership_id=recipient_membership_id,
            expires_at=expires_at,
        )
        self._outbox.insert_action_token(
            ActionTokenRecord(
                id=uuid.uuid4(),
                circle_id=circle_id,
                token_hash=tok_hash,
                key_id=self._cfg.action_signing_key_id,
                action_type=action,
                resource_type=resource_type,
                resource_id=resource_id,
                recipient_membership_id=recipient_membership_id,
                expires_at=expires_at,
                created_at=now,
            )
        )
        return raw_token

    def _validate_and_authorize(self, raw_token: str) -> tuple[ActionTokenRecord, ActorContext]:
        now = self._clock.now()
        payload = verify_action_token(
            raw_token,
            now=now,
            active_key_id=self._cfg.action_signing_key_id,
            active_key=self._cfg.action_signing_key,
            previous_key_id=self._cfg.action_signing_previous_key_id,
            previous_key=self._cfg.action_signing_previous_key,
        )
        tok_hash = hash_token(raw_token)
        record = self._outbox.get_action_token_by_hash(tok_hash)
        if not record or record.consumed_at is not None or record.invalidated_at is not None:
            raise TokenInvalidError("errors.token_invalid")
        if record.expires_at <= now:
            raise TokenInvalidError("errors.token_expired")
        if (
            record.action_type != payload.act
            or record.circle_id != payload.cid
            or record.resource_type != payload.rt
            or record.resource_id != payload.rid
            or record.recipient_membership_id != payload.mid
        ):
            raise TokenInvalidError("errors.token_invalid")

        mem = self._circle.get_membership(record.circle_id, record.recipient_membership_id)
        circle = self._circle.get_circle(record.circle_id)
        if not mem or not mem.active or not circle:
            raise AuthorizationError("errors.forbidden")
        acc = self._auth.get_account_by_id(mem.account_id)
        if not acc:
            raise AuthorizationError("errors.forbidden")

        consents = self._circle.get_member_consents(record.circle_id, mem.id)
        actor = ActorContext(
            session_id=uuid.uuid4(),
            account_id=acc.id,
            membership_id=mem.id,
            circle_id=circle.id,
            role=mem.role,
            display_name=acc.display_name,
            language=acc.language,
            font_px=acc.font_px,
            high_contrast=acc.high_contrast,
            is_care_contact=mem.is_care_contact,
            grants=mem.grants,
            consents=consents,
            auth_revision=mem.auth_revision,
            circle_timezone=circle.timezone,
            ai_consent_granted=circle.ai_consent_granted,
        )

        # Re-check permissions & category consent at preview/redemption time
        if record.action_type in (
            ActionTokenType.DOSE_TAKEN,
            ActionTokenType.DOSE_SNOOZE,
            ActionTokenType.DOSE_CANT_TAKE,
        ):
            authorize(actor, Action.LOG_DOSE, target_circle_id=record.circle_id)
        elif record.action_type == ActionTokenType.CARE_ACK:
            authorize(
                actor,
                Action.ACKNOWLEDGE_CARE,
                target_circle_id=record.circle_id,
                required_categories=(ConsentCategory.MEDICATIONS,),
            )
        elif record.action_type in (
            ActionTokenType.CHECKIN_GOOD,
            ActionTokenType.CHECKIN_OKAY,
            ActionTokenType.CHECKIN_NOT_WELL,
        ):
            authorize(actor, Action.SUBMIT_CHECKIN, target_circle_id=record.circle_id)
        elif record.action_type == ActionTokenType.CHECKIN_ACK:
            authorize(
                actor,
                Action.ACKNOWLEDGE_CARE,
                target_circle_id=record.circle_id,
                required_categories=(ConsentCategory.CHECKINS,),
            )

        return record, actor

    def preview_token(self, raw_token: str) -> ActionTokenPreview:
        """Harmless GET preview for email action links; never mutates state."""
        record, _ = self._validate_and_authorize(raw_token)
        circle = self._circle.get_circle(record.circle_id)
        if not circle:
            raise NotFoundError("errors.not_found")
        context: dict[str, Any] = {}
        if record.resource_type == "dose":
            dose = self._dose.get_dose(record.circle_id, record.resource_id)
            if not dose or dose.state in (DoseState.CANCELLED, DoseState.EXPIRED):
                raise TokenInvalidError("errors.token_expired")
            context = {
                "medication_name": dose.medication_name,
                "dose_text": dose.dose_text,
                "due_at": dose.due_at,
                "timezone": circle.timezone,
                "state": dose.state.value,
            }
        elif record.resource_type == "checkin":
            ci = self._collab.get_checkin_by_id(record.resource_id)
            if not ci or ci.circle_id != record.circle_id:
                raise NotFoundError("errors.not_found")
            context = {"local_date": ci.local_date, "timezone": circle.timezone}
        return ActionTokenPreview(
            action=record.action_type,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            circle_id=record.circle_id,
            recipient_membership_id=record.recipient_membership_id,
            elder_name=circle.display_name,
            context=context,
        )

    def redeem_token(
        self,
        raw_token: str,
        *,
        cant_take_reason: CantTakeReason | None = None,
        cant_take_note: str = "",
    ) -> dict[str, Any]:
        """Atomically consume token after rechecking consent/status and execute action."""
        record, actor = self._validate_and_authorize(raw_token)
        now = self._clock.now()

        # Pre-check resource eligibility before consuming token
        if record.resource_type == "dose":
            dose = self._dose.get_dose(record.circle_id, record.resource_id)
            if not dose:
                raise NotFoundError("errors.not_found")
            if dose.expires_at <= now or dose.state in (DoseState.CANCELLED, DoseState.EXPIRED):
                raise TokenInvalidError("errors.token_expired")
            if record.action_type == ActionTokenType.DOSE_CANT_TAKE:
                if cant_take_reason is None:
                    raise ValidationError("errors.validation")
                if cant_take_reason == CantTakeReason.OTHER and not cant_take_note.strip():
                    raise ValidationError("errors.cant_take_note_required")

        if not self._outbox.consume_action_token(token_hash=record.token_hash, now=now):
            raise TokenInvalidError("errors.token_invalid")

        result: dict[str, Any] = {"action": record.action_type.value}
        if record.action_type == ActionTokenType.DOSE_TAKEN:
            updated, already, disc = self._dose.transition_taken(
                circle_id=record.circle_id,
                dose_id=record.resource_id,
                actor_account_id=actor.account_id,
                now=now,
            )
            result.update(
                {"dose_state": updated.state.value, "already": already, "discrepancy": disc}
            )
        elif record.action_type == ActionTokenType.DOSE_SNOOZE:
            updated = self._dose.transition_snooze(
                circle_id=record.circle_id,
                dose_id=record.resource_id,
                actor_account_id=actor.account_id,
                snoozed_until=now + timedelta(minutes=10),
                now=now,
            )
            result.update({"dose_state": updated.state.value})
        elif record.action_type == ActionTokenType.DOSE_CANT_TAKE:
            assert cant_take_reason is not None
            updated = self._dose.transition_cant_take(
                circle_id=record.circle_id,
                dose_id=record.resource_id,
                actor_account_id=actor.account_id,
                reason=cant_take_reason.value,
                note=cant_take_note.strip()[:300],
                report_id=uuid.uuid4(),
                now=now,
            )
            result.update({"dose_state": updated.state.value})
        elif record.action_type in (ActionTokenType.CARE_ACK, ActionTokenType.CHECKIN_ACK):
            ep_type = "dose" if record.action_type == ActionTokenType.CARE_ACK else "checkin"
            ack, is_first = self._collab.record_acknowledgement(
                circle_id=record.circle_id,
                episode_type=ep_type,
                resource_id=record.resource_id,
                membership_id=actor.membership_id,
                actor_name=actor.display_name,
                now=now,
            )
            result.update({"acknowledged_by": ack.acknowledged_by_name, "first": is_first})
        elif record.action_type in (
            ActionTokenType.CHECKIN_GOOD,
            ActionTokenType.CHECKIN_OKAY,
            ActionTokenType.CHECKIN_NOT_WELL,
        ):
            mood_map = {
                ActionTokenType.CHECKIN_GOOD: CheckInMood.GOOD,
                ActionTokenType.CHECKIN_OKAY: CheckInMood.OKAY,
                ActionTokenType.CHECKIN_NOT_WELL: CheckInMood.NOT_WELL,
            }
            ci, _ = self._collab.submit_checkin_response(
                circle_id=record.circle_id,
                checkin_id=record.resource_id,
                mood=mood_map[record.action_type],
                note="",
                actor_account_id=actor.account_id,
                now=now,
            )
            result.update({"mood": ci.mood.value if ci.mood else ""})

        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=record.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="action_token_redeemed",
                target_type=record.resource_type,
                target_id=str(record.resource_id),
                metadata={"action": record.action_type.value},
                occurred_at=now,
            )
        )
        _LOG.info("action_token_redeemed", action=record.action_type.value)
        return result
