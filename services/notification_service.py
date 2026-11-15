"""Transactional notification outbox, consent/language rendering, dispatch, and reconciliation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.clock import Clock
from core.config import AppConfig
from core.errors import NotFoundError
from core.i18n import format_local_datetime, format_local_time, t
from core.logging import get_logger
from core.models import (
    ActionTokenType,
    ActorContext,
    CareCircle,
    CheckInMood,
    ConsentCategory,
    DoseState,
    NotificationChannel,
    OutboxStatus,
    Role,
)
from core.models_clinical import (
    Appointment,
    AuditEvent,
    CareAcknowledgement,
    CheckInInstance,
    DoseInstance,
    Medication,
    NotificationOutboxItem,
)
from core.permissions import Action, authorize
from core.security import escape_html
from integrations.email_gateway import DeliveryState, EmailGateway, EmailPayload
from integrations.telegram_gateway import TelegramGateway, TelegramPayload
from repositories.interfaces import (
    AuthRepository,
    CircleRepository,
    CollabRepository,
    DoseRepository,
    OutboxRepository,
)

from services.action_token_service import ActionTokenService

_LOG = get_logger("carecircle.outbox")
_MAX_ATTEMPTS = 4


class NotificationService:
    """Manages transactional outbox queueing, pre-send rechecks, dispatch, and care acks."""

    def __init__(
        self,
        *,
        auth_repo: AuthRepository,
        circle_repo: CircleRepository,
        dose_repo: DoseRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        token_service: ActionTokenService,
        email_gateway: EmailGateway,
        telegram_gateway: TelegramGateway,
        config: AppConfig,
        clock: Clock,
    ) -> None:
        self._auth = auth_repo
        self._circle = circle_repo
        self._dose = dose_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._tokens = token_service
        self._email = email_gateway
        self._telegram = telegram_gateway
        self._cfg = config
        self._clock = clock

    def _elder_membership_id(self, circle_id: UUID) -> UUID | None:
        for mem, _, _ in self._circle.list_members(circle_id):
            if mem.role == Role.ELDER and mem.active:
                return mem.id
        return None

    def _recipient_channels(
        self,
        circle_id: UUID,
        membership_id: UUID,
        required_category: ConsentCategory | None,
    ) -> list[NotificationChannel]:
        mem = self._circle.get_membership(circle_id, membership_id)
        if not mem or not mem.active:
            return []
        if required_category is not None and mem.role != Role.ELDER:
            consents = self._circle.get_member_consents(circle_id, membership_id)
            if required_category not in consents:
                return []
        channels = [NotificationChannel.EMAIL]
        acc = self._auth.get_account_by_id(mem.account_id)
        if acc and acc.telegram_opt_in and acc.telegram_chat_id and self._cfg.has_telegram:
            channels.append(NotificationChannel.TELEGRAM)
        return channels

    def _enqueue_for_member(
        self,
        *,
        circle: CareCircle,
        logical_prefix: str,
        event_type: str,
        resource_type: str,
        resource_id: UUID,
        escalation_level: int,
        recipient_membership_id: UUID,
        required_category: ConsentCategory | None,
        resource_version: int,
        payload: dict[str, Any],
    ) -> list[NotificationOutboxItem]:
        now = self._clock.now()
        created: list[NotificationOutboxItem] = []
        for ch in self._recipient_channels(circle.id, recipient_membership_id, required_category):
            lkey = f"{logical_prefix}:{recipient_membership_id}:{ch.value}"
            item = NotificationOutboxItem(
                id=uuid.uuid4(),
                circle_id=circle.id,
                logical_key=lkey,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                escalation_level=escalation_level,
                recipient_membership_id=recipient_membership_id,
                channel=ch,
                required_category=required_category,
                resource_version=resource_version,
                status=OutboxStatus.PENDING,
                next_attempt_at=now,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
            saved, _ = self._outbox.enqueue_outbox(item)
            created.append(saved)
        return created

    def queue_dose_escalation(
        self,
        *,
        circle: CareCircle,
        dose: DoseInstance,
        level: int,
        target_role: str,
    ) -> list[NotificationOutboxItem]:
        """Enqueue a Care Chain dose reminder or respectful contact notification."""
        if target_role == "elder":
            recipient = self._elder_membership_id(circle.id)
            req_cat = None
        elif target_role == "primary_contact":
            recipient = circle.primary_contact_membership_id
            req_cat = ConsentCategory.MEDICATIONS
        else:
            recipient = circle.secondary_contact_membership_id
            req_cat = ConsentCategory.MEDICATIONS

        if recipient is None:
            self._outbox.append_audit(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle.id,
                    operation="escalation_contact_missing_or_unconsented",
                    target_type="dose",
                    target_id=str(dose.id),
                    metadata={"level": level, "target_role": target_role},
                    occurred_at=self._clock.now(),
                )
            )
            return []

        items = self._enqueue_for_member(
            circle=circle,
            logical_prefix=f"dose:{dose.id}:lvl{level}",
            event_type=f"dose_escalation_lvl{level}",
            resource_type="dose",
            resource_id=dose.id,
            escalation_level=level,
            recipient_membership_id=recipient,
            required_category=req_cat,
            resource_version=1,
            payload={
                "target_role": target_role,
                "elder_name": circle.display_name,
                "med_name": dose.medication_name,
                "dose_text": dose.dose_text,
                "due_at_iso": dose.due_at.isoformat(),
                "expires_at_iso": dose.expires_at.isoformat(),
                "timezone": circle.timezone,
            },
        )
        if not items:
            self._outbox.append_audit(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle.id,
                    operation="escalation_skipped_unconsented",
                    target_type="dose",
                    target_id=str(dose.id),
                    metadata={"level": level, "target_role": target_role},
                    occurred_at=self._clock.now(),
                )
            )
        return items

    def queue_and_dispatch_cant_take(self, *, circle: CareCircle, dose: DoseInstance) -> str:
        """Transactionally queue explicit can't-take alert and attempt bounded post-commit send."""
        recipient = circle.primary_contact_membership_id
        if not recipient:
            return "no_contact_configured"
        rep_id = str(dose.cant_take_report_id or dose.id)
        items = self._enqueue_for_member(
            circle=circle,
            logical_prefix=f"dose_cant_take:{dose.id}:{rep_id}",
            event_type="dose_cant_take_report",
            resource_type="dose",
            resource_id=dose.id,
            escalation_level=3,
            recipient_membership_id=recipient,
            required_category=ConsentCategory.MEDICATIONS,
            resource_version=1,
            payload={
                "elder_name": circle.display_name,
                "med_name": dose.medication_name,
                "dose_text": dose.dose_text,
                "due_at_iso": dose.due_at.isoformat(),
                "expires_at_iso": dose.expires_at.isoformat(),
                "timezone": circle.timezone,
                "reason": (
                    getattr(dose.cant_take_reason, "value", dose.cant_take_reason)
                    if dose.cant_take_reason
                    else "other"
                ),
            },
        )
        if not items:
            return "skipped_unconsented"
        return self.dispatch_specific_items(items)

    def queue_and_dispatch_not_well(self, *, circle: CareCircle, checkin: CheckInInstance) -> str:
        """Create or reactivate logical Not well alert within window and dispatch."""
        recipient = circle.primary_contact_membership_id or circle.checkin_fallback_membership_id
        if not recipient:
            return "no_contact_configured"
        channels = self._recipient_channels(circle.id, recipient, ConsentCategory.CHECKINS)
        if not channels and circle.checkin_fallback_membership_id:
            recipient = circle.checkin_fallback_membership_id
            channels = self._recipient_channels(circle.id, recipient, ConsentCategory.CHECKINS)
        if not channels:
            return "skipped_unconsented"

        now = self._clock.now()
        payload = {
            "elder_name": circle.display_name,
            "local_date": checkin.local_date.isoformat(),
            "timezone": circle.timezone,
            "expires_at_iso": checkin.expires_at.isoformat(),
        }
        items: list[NotificationOutboxItem] = []
        for ch in channels:
            lkey = f"checkin_not_well:{checkin.id}:{recipient}:{ch.value}"
            existing = self._outbox.get_outbox_by_logical_key(lkey)
            if existing:
                if existing.status == OutboxStatus.CANCELLED:
                    reactivated = self._outbox.reactivate_outbox_item(
                        logical_key=lkey, payload=payload, now=now
                    )
                    if reactivated:
                        items.append(reactivated)
            else:
                items.extend(
                    self._enqueue_for_member(
                        circle=circle,
                        logical_prefix=f"checkin_not_well:{checkin.id}",
                        event_type="checkin_not_well_report",
                        resource_type="checkin",
                        resource_id=checkin.id,
                        escalation_level=3,
                        recipient_membership_id=recipient,
                        required_category=ConsentCategory.CHECKINS,
                        resource_version=1,
                        payload=payload,
                    )
                )
        if not items:
            return "already_processed"
        return self.dispatch_specific_items(items)

    def cancel_unsent_not_well(self, *, checkin_id: UUID, now: datetime) -> int:
        """Cancel unsent Not well events when elder updates mood away from Not well."""
        return self._outbox.cancel_outbox_for_resource(
            resource_id=checkin_id, event_prefix="checkin_not_well_report", now=now
        )

    def queue_checkin_escalation(
        self,
        *,
        circle: CareCircle,
        checkin: CheckInInstance,
        level: int,
        target_role: str,
    ) -> list[NotificationOutboxItem]:
        """Enqueue unanswered check-in reminder or gentle contact notification."""
        if target_role == "elder":
            recipient = self._elder_membership_id(circle.id)
            req_cat = None
        else:
            recipient = circle.primary_contact_membership_id
            req_cat = ConsentCategory.CHECKINS
        if not recipient:
            return []
        return self._enqueue_for_member(
            circle=circle,
            logical_prefix=f"checkin:{checkin.id}:lvl{level}",
            event_type=f"checkin_unanswered_lvl{level}",
            resource_type="checkin",
            resource_id=checkin.id,
            escalation_level=level,
            recipient_membership_id=recipient,
            required_category=req_cat,
            resource_version=1,
            payload={
                "elder_name": circle.display_name,
                "due_at_iso": checkin.due_at.isoformat(),
                "expires_at_iso": checkin.expires_at.isoformat(),
                "timezone": circle.timezone,
            },
        )

    def queue_and_dispatch_urgent_help(
        self, *, circle: CareCircle, event_id: UUID, triggered_at: datetime
    ) -> str:
        """Queue and attempt immediate dispatch for an explicit urgent-help request."""
        recipients = [
            rid
            for rid in (
                circle.primary_contact_membership_id,
                circle.secondary_contact_membership_id,
                circle.checkin_fallback_membership_id,
            )
            if rid is not None
        ]
        if not recipients:
            return "no_contact_configured"
        items: list[NotificationOutboxItem] = []
        for rid in dict.fromkeys(recipients):
            items.extend(
                self._enqueue_for_member(
                    circle=circle,
                    logical_prefix=f"urgent_help:{event_id}",
                    event_type="urgent_help_alert",
                    resource_type="urgent_help",
                    resource_id=event_id,
                    escalation_level=4,
                    recipient_membership_id=rid,
                    required_category=None,
                    resource_version=1,
                    payload={
                        "elder_name": circle.display_name,
                        "triggered_at_iso": triggered_at.isoformat(),
                        "timezone": circle.timezone,
                    },
                )
            )
        if not items:
            return "no_contact_configured"
        return self.dispatch_specific_items(items)

    def queue_appointment_reminder(
        self, *, circle: CareCircle, appointment: Appointment, level: int
    ) -> list[NotificationOutboxItem]:
        """Enqueue 24h (level=1) or 1h (level=2) appointment reminder bound to version."""
        recipients: list[tuple[UUID, ConsentCategory | None]] = []
        elder_id = self._elder_membership_id(circle.id)
        if elder_id:
            recipients.append((elder_id, None))
        if circle.primary_contact_membership_id:
            recipients.append((circle.primary_contact_membership_id, ConsentCategory.APPOINTMENTS))
        items: list[NotificationOutboxItem] = []
        for rid, cat in recipients:
            items.extend(
                self._enqueue_for_member(
                    circle=circle,
                    logical_prefix=f"appt:{appointment.id}:v{appointment.version}:lvl{level}",
                    event_type="appointment_reminder",
                    resource_type="appointment",
                    resource_id=appointment.id,
                    escalation_level=level,
                    recipient_membership_id=rid,
                    required_category=cat,
                    resource_version=appointment.version,
                    payload={
                        "elder_name": circle.display_name,
                        "doctor_name": appointment.doctor_name,
                        "location": appointment.location,
                        "starts_at_iso": appointment.starts_at.isoformat(),
                        "timezone": appointment.timezone,
                    },
                )
            )
        return items

    def queue_low_stock_alert(
        self, *, circle: CareCircle, medication: Medication
    ) -> list[NotificationOutboxItem]:
        """Queue low-stock alert for consented configured recipients."""
        recipients = [
            rid
            for rid in (
                self._elder_membership_id(circle.id),
                circle.primary_contact_membership_id,
            )
            if rid is not None
        ]
        items: list[NotificationOutboxItem] = []
        for rid in dict.fromkeys(recipients):
            items.extend(
                self._enqueue_for_member(
                    circle=circle,
                    logical_prefix=f"low_stock:{medication.id}:v{medication.version}:{int(medication.stock_count or 0)}",
                    event_type="low_stock_alert",
                    resource_type="medication",
                    resource_id=medication.id,
                    escalation_level=1,
                    recipient_membership_id=rid,
                    required_category=ConsentCategory.MEDICATIONS,
                    resource_version=medication.version,
                    payload={
                        "med_name": medication.name,
                        "count": medication.stock_count,
                        "unit": medication.stock_unit,
                        "threshold": medication.refill_threshold,
                    },
                )
            )
        return items

    def acknowledge_episode(
        self, actor: ActorContext, *, episode_type: str, resource_id: UUID
    ) -> tuple[CareAcknowledgement, bool]:
        """Atomically record first care acknowledgement, stop episode escalation, and notify contacts."""
        authorize(actor, Action.ACKNOWLEDGE_CARE, target_circle_id=actor.circle_id)
        now = self._clock.now()
        ack, is_first = self._collab.record_acknowledgement(
            circle_id=actor.circle_id,
            episode_type=episode_type,
            resource_id=resource_id,
            membership_id=actor.membership_id,
            actor_name=actor.display_name,
            now=now,
        )
        if is_first:
            circle = self._circle.get_circle(actor.circle_id)
            if circle:
                other_contacts = [
                    rid
                    for rid in (
                        circle.primary_contact_membership_id,
                        circle.secondary_contact_membership_id,
                        circle.checkin_fallback_membership_id,
                    )
                    if rid is not None and rid != actor.membership_id
                ]
                items: list[NotificationOutboxItem] = []
                for rid in dict.fromkeys(other_contacts):
                    items.extend(
                        self._enqueue_for_member(
                            circle=circle,
                            logical_prefix=f"ack_notice:{episode_type}:{resource_id}",
                            event_type="care_ack_notice",
                            resource_type=episode_type,
                            resource_id=resource_id,
                            escalation_level=0,
                            recipient_membership_id=rid,
                            required_category=None,
                            resource_version=1,
                            payload={
                                "actor_name": actor.display_name,
                                "elder_name": circle.display_name,
                            },
                        )
                    )
                if items:
                    self.dispatch_specific_items(items)
        return ack, is_first

    def _is_still_eligible(self, item: NotificationOutboxItem) -> bool:
        now = self._clock.now()
        circle = self._circle.get_circle(item.circle_id)
        if not circle:
            return False
        mem = self._circle.get_membership(item.circle_id, item.recipient_membership_id)
        if not mem or not mem.active:
            return False
        if (
            item.required_category is not None
            and mem.role != Role.ELDER
            and item.required_category
            not in self._circle.get_member_consents(item.circle_id, mem.id)
        ):
            return False

        if item.event_type.startswith("dose_escalation_"):
            dose = self._dose.get_dose(item.circle_id, item.resource_id)
            if (
                not dose
                or dose.state not in (DoseState.PENDING, DoseState.SNOOZED)
                or dose.expires_at <= now
            ):
                return False
            if self._collab.get_acknowledgement(episode_type="dose", resource_id=dose.id):
                return False
        elif item.event_type.startswith("checkin_unanswered_"):
            ci = self._collab.get_checkin_by_id(item.resource_id)
            if not ci or ci.responded_at is not None or ci.expires_at <= now:
                return False
            if self._collab.get_acknowledgement(episode_type="checkin", resource_id=ci.id):
                return False
        elif item.event_type == "checkin_not_well_report":
            ci = self._collab.get_checkin_by_id(item.resource_id)
            if not ci or ci.mood != CheckInMood.NOT_WELL or ci.expires_at <= now:
                return False
        elif item.event_type == "appointment_reminder":
            appt = self._collab.get_appointment(item.circle_id, item.resource_id)
            if (
                not appt
                or appt.cancelled_at is not None
                or appt.version != item.resource_version
                or appt.starts_at <= now
            ):
                return False
        return True

    def _render_and_send(self, item: NotificationOutboxItem) -> DeliveryState:
        mem = self._circle.get_membership(item.circle_id, item.recipient_membership_id)
        if not mem:
            return DeliveryState.DEFINITIVE_FAILURE
        acc = self._auth.get_account_by_id(mem.account_id)
        if not acc:
            return DeliveryState.DEFINITIVE_FAILURE
        lang = acc.language if acc.language in ("en", "hi") else "en"
        p = item.payload
        tz = str(p.get("timezone", "Asia/Kolkata"))

        subject = "CareCircle"
        body = ""
        links: list[tuple[str, str]] = []

        if item.event_type.startswith("dose_escalation_"):
            due_dt = datetime.fromisoformat(str(p["due_at_iso"]))
            exp_dt = datetime.fromisoformat(str(p["expires_at_iso"]))
            time_str = format_local_time(due_dt, tz, lang=lang)
            if p.get("target_role") == "elder":
                subject = t(
                    "notifications.dose_elder_subject", lang=lang, med=p["med_name"], time=time_str
                )
                body = t(
                    "notifications.dose_elder_body",
                    lang=lang,
                    elder=p["elder_name"],
                    med=p["med_name"],
                    dose=p["dose_text"],
                    time=time_str,
                )
                for act, lbl_key in (
                    (ActionTokenType.DOSE_TAKEN, "elder_mode.taken_button"),
                    (ActionTokenType.DOSE_SNOOZE, "elder_mode.snooze_button"),
                    (ActionTokenType.DOSE_CANT_TAKE, "elder_mode.cant_take_button"),
                ):
                    raw_tok = self._tokens.issue_token_in_memory(
                        action=act,
                        circle_id=item.circle_id,
                        resource_type="dose",
                        resource_id=item.resource_id,
                        recipient_membership_id=item.recipient_membership_id,
                        window_ends_at=exp_dt,
                    )
                    links.append(
                        (t(lbl_key, lang=lang), f"{self._cfg.app_url}/?action_token={raw_tok}")
                    )
            else:
                subject = t(
                    "notifications.dose_contact_subject",
                    lang=lang,
                    elder=p["elder_name"],
                    med=p["med_name"],
                )
                body = t(
                    "notifications.dose_contact_body",
                    lang=lang,
                    elder=p["elder_name"],
                    med=p["med_name"],
                    dose=p["dose_text"],
                    time=time_str,
                )
                raw_tok = self._tokens.issue_token_in_memory(
                    action=ActionTokenType.CARE_ACK,
                    circle_id=item.circle_id,
                    resource_type="dose",
                    resource_id=item.resource_id,
                    recipient_membership_id=item.recipient_membership_id,
                    window_ends_at=exp_dt,
                )
                links.append(
                    (
                        t("care_chain.ack_button", lang=lang),
                        f"{self._cfg.app_url}/?action_token={raw_tok}",
                    )
                )
        elif item.event_type == "dose_cant_take_report":
            due_dt = datetime.fromisoformat(str(p["due_at_iso"]))
            exp_dt = datetime.fromisoformat(str(p["expires_at_iso"]))
            time_str = format_local_time(due_dt, tz, lang=lang)
            reason_label = t(f"cant_take_reasons.{p.get('reason', 'other')}", lang=lang)
            subject = t(
                "notifications.dose_cant_take_subject",
                lang=lang,
                elder=p["elder_name"],
                med=p["med_name"],
            )
            body = t(
                "notifications.dose_cant_take_body",
                lang=lang,
                elder=p["elder_name"],
                med=p["med_name"],
                dose=p["dose_text"],
                time=time_str,
                reason=reason_label,
            )
            raw_tok = self._tokens.issue_token_in_memory(
                action=ActionTokenType.CARE_ACK,
                circle_id=item.circle_id,
                resource_type="dose",
                resource_id=item.resource_id,
                recipient_membership_id=item.recipient_membership_id,
                window_ends_at=exp_dt,
            )
            links.append(
                (
                    t("care_chain.ack_button", lang=lang),
                    f"{self._cfg.app_url}/?action_token={raw_tok}",
                )
            )
        elif item.event_type == "care_ack_notice":
            subject = t(
                "notifications.ack_subject", lang=lang, actor=p["actor_name"], elder=p["elder_name"]
            )
            body = t(
                "notifications.ack_body", lang=lang, actor=p["actor_name"], elder=p["elder_name"]
            )
        elif item.event_type.startswith("checkin_unanswered_"):
            due_dt = datetime.fromisoformat(str(p["due_at_iso"]))
            exp_dt = datetime.fromisoformat(str(p["expires_at_iso"]))
            time_str = format_local_time(due_dt, tz, lang=lang)
            if item.escalation_level == 1:
                subject = t("notifications.checkin_elder_subject", lang=lang)
                body = t("notifications.checkin_elder_body", lang=lang, elder=p["elder_name"])
                for act, lbl_key in (
                    (ActionTokenType.CHECKIN_GOOD, "elder_mode.checkin_good_button"),
                    (ActionTokenType.CHECKIN_OKAY, "elder_mode.checkin_okay_button"),
                    (ActionTokenType.CHECKIN_NOT_WELL, "elder_mode.checkin_not_well_button"),
                ):
                    raw_tok = self._tokens.issue_token_in_memory(
                        action=act,
                        circle_id=item.circle_id,
                        resource_type="checkin",
                        resource_id=item.resource_id,
                        recipient_membership_id=item.recipient_membership_id,
                        window_ends_at=exp_dt,
                    )
                    links.append(
                        (t(lbl_key, lang=lang), f"{self._cfg.app_url}/?action_token={raw_tok}")
                    )
            else:
                subject = t(
                    "notifications.checkin_unanswered_subject", lang=lang, elder=p["elder_name"]
                )
                body = t(
                    "notifications.checkin_unanswered_body",
                    lang=lang,
                    elder=p["elder_name"],
                    time=time_str,
                )
                raw_tok = self._tokens.issue_token_in_memory(
                    action=ActionTokenType.CHECKIN_ACK,
                    circle_id=item.circle_id,
                    resource_type="checkin",
                    resource_id=item.resource_id,
                    recipient_membership_id=item.recipient_membership_id,
                    window_ends_at=exp_dt,
                )
                links.append(
                    (
                        t("care_chain.ack_button", lang=lang),
                        f"{self._cfg.app_url}/?action_token={raw_tok}",
                    )
                )
        elif item.event_type == "checkin_not_well_report":
            exp_dt = datetime.fromisoformat(str(p["expires_at_iso"]))
            subject = t("notifications.checkin_not_well_subject", lang=lang, elder=p["elder_name"])
            body = t("notifications.checkin_not_well_body", lang=lang, elder=p["elder_name"])
            raw_tok = self._tokens.issue_token_in_memory(
                action=ActionTokenType.CHECKIN_ACK,
                circle_id=item.circle_id,
                resource_type="checkin",
                resource_id=item.resource_id,
                recipient_membership_id=item.recipient_membership_id,
                window_ends_at=exp_dt,
            )
            links.append(
                (
                    t("care_chain.ack_button", lang=lang),
                    f"{self._cfg.app_url}/?action_token={raw_tok}",
                )
            )
        elif item.event_type == "urgent_help_alert":
            trig_dt = datetime.fromisoformat(str(p["triggered_at_iso"]))
            time_str = format_local_datetime(trig_dt, tz, lang=lang)
            subject = t("notifications.urgent_help_subject", lang=lang, elder=p["elder_name"])
            body = t(
                "notifications.urgent_help_body", lang=lang, elder=p["elder_name"], time=time_str
            )
        elif item.event_type == "appointment_reminder":
            starts_dt = datetime.fromisoformat(str(p["starts_at_iso"]))
            time_str = format_local_datetime(starts_dt, tz, lang=lang)
            subject = t(
                "notifications.appointment_subject",
                lang=lang,
                doctor=p["doctor_name"],
                time=time_str,
            )
            body = t(
                "notifications.appointment_body",
                lang=lang,
                elder=p["elder_name"],
                doctor=p["doctor_name"],
                location=p.get("location", ""),
                time=time_str,
            )
        elif item.event_type == "low_stock_alert":
            subject = t("notifications.low_stock_subject", lang=lang, med=p["med_name"])
            body = t(
                "notifications.low_stock_body",
                lang=lang,
                med=p["med_name"],
                count=p["count"],
                unit=p.get("unit", ""),
                threshold=p["threshold"],
            )

        if item.channel == NotificationChannel.TELEGRAM:
            chat_id = acc.telegram_chat_id or ""
            tg_text = f"{subject}\n{body}" + (
                "\n" + "\n".join(f"{lbl}: {url}" for lbl, url in links) if links else ""
            )
            res = self._telegram.send(
                TelegramPayload(chat_id=chat_id, text=tg_text, client_ref=str(item.id))
            )
        else:
            text_lines = [body] + [f"{lbl}: {url}" for lbl, url in links]
            btn_html = "".join(
                f"<p><a href='{escape_html(url)}' style='display:inline-block;padding:14px 22px;background:#0F5C54;color:#ffffff;text-decoration:none;border-radius:6px;font-size:18px;font-weight:bold;'>{escape_html(lbl)}</a></p>"
                for lbl, url in links
            )
            html_body = f"<div style='font-family:sans-serif;font-size:18px;line-height:1.5;color:#141A19;'><p>{escape_html(body)}</p>{btn_html}</div>"
            res = self._email.send(
                EmailPayload(
                    to_address=acc.email_normalized,
                    subject=subject,
                    text_body="\n\n".join(text_lines),
                    html_body=html_body,
                    message_id=f"<cc-{item.id}-a{item.attempts + 1}@carecircle.local>",
                )
            )

        now = self._clock.now()
        if res.state == DeliveryState.ACCEPTED:
            self._outbox.update_outbox_outcome(
                item_id=item.id,
                status=OutboxStatus.SENT,
                now=now,
                attempts=item.attempts + 1,
                provider_message_id=res.provider_message_id,
            )
        elif res.state == DeliveryState.UNCERTAIN:
            # Preserve tokens after uncertain send; never blind-retry
            self._outbox.update_outbox_outcome(
                item_id=item.id,
                status=OutboxStatus.UNCERTAIN,
                now=now,
                attempts=item.attempts + 1,
                error_code=res.error_code,
                provider_message_id=res.provider_message_id,
            )
        else:
            # Pre-accept definitive failure: invalidate tokens from this attempt
            self._outbox.invalidate_action_tokens_for_resource(
                resource_id=item.resource_id, now=now
            )
            new_attempts = item.attempts + 1
            if new_attempts >= _MAX_ATTEMPTS:
                self._outbox.update_outbox_outcome(
                    item_id=item.id,
                    status=OutboxStatus.FAILED,
                    now=now,
                    attempts=new_attempts,
                    error_code=res.error_code,
                )
            else:
                backoff = timedelta(minutes=2**new_attempts)
                self._outbox.update_outbox_outcome(
                    item_id=item.id,
                    status=OutboxStatus.RETRYABLE,
                    now=now,
                    attempts=new_attempts,
                    next_attempt_at=now + backoff,
                    error_code=res.error_code,
                )
        return res.state

    def dispatch_specific_items(self, items: list[NotificationOutboxItem]) -> str:
        """Bounded fast-path dispatch through the same outbox claim/eligibility rules."""
        worker_id = f"fast-{uuid.uuid4().hex[:8]}"
        now = self._clock.now()
        claimed = self._outbox.claim_outbox_batch(
            worker_id=worker_id, now=now, lease_seconds=30, batch_size=max(len(items), 10)
        )
        target_ids = {i.id for i in items}
        last_status = "queued"
        for c in claimed:
            if not self._is_still_eligible(c):
                self._outbox.update_outbox_outcome(
                    item_id=c.id, status=OutboxStatus.CANCELLED, now=now
                )
                continue
            if not self._outbox.mark_outbox_sending(item_id=c.id, lease_owner=worker_id, now=now):
                continue
            outcome = self._render_and_send(c)
            if c.id in target_ids:
                last_status = (
                    "provider_accepted"
                    if outcome == DeliveryState.ACCEPTED
                    else "uncertain"
                    if outcome == DeliveryState.UNCERTAIN
                    else "failed_retryable"
                )
        return last_status

    def process_due_outbox(self, *, worker_id: str, batch_size: int = 50) -> dict[str, int]:
        """Claim and process due outbox rows atomically with lease protection."""
        now = self._clock.now()
        claimed = self._outbox.claim_outbox_batch(
            worker_id=worker_id, now=now, lease_seconds=60, batch_size=batch_size
        )
        counts = {
            "claimed": len(claimed),
            "sent": 0,
            "cancelled": 0,
            "uncertain": 0,
            "retryable": 0,
        }
        for item in claimed:
            if not self._is_still_eligible(item):
                self._outbox.update_outbox_outcome(
                    item_id=item.id, status=OutboxStatus.CANCELLED, now=self._clock.now()
                )
                counts["cancelled"] += 1
                continue
            if not self._outbox.mark_outbox_sending(
                item_id=item.id, lease_owner=worker_id, now=self._clock.now()
            ):
                continue
            outcome = self._render_and_send(item)
            if outcome == DeliveryState.ACCEPTED:
                counts["sent"] += 1
            elif outcome == DeliveryState.UNCERTAIN:
                counts["uncertain"] += 1
            else:
                counts["retryable"] += 1
        return counts

    def reconcile_uncertain(
        self, actor: ActorContext, *, item_id: UUID, resolution: str
    ) -> NotificationOutboxItem:
        """Allow authorized elder to reconcile an uncertain send as sent or retryable."""
        authorize(actor, Action.RECONCILE_OUTBOX, target_circle_id=actor.circle_id)
        now = self._clock.now()
        items = self._outbox.list_outbox(actor.circle_id, limit=200)
        target = next((o for o in items if o.id == item_id), None)
        if not target or target.status != OutboxStatus.UNCERTAIN:
            raise NotFoundError("errors.not_found")
        new_status = OutboxStatus.SENT if resolution == "sent" else OutboxStatus.RETRYABLE
        updated = self._outbox.update_outbox_outcome(
            item_id=item_id, status=new_status, now=now, next_attempt_at=now
        )
        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=actor.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="outbox_uncertain_reconciled",
                target_type="outbox",
                target_id=str(item_id),
                metadata={"resolution": resolution},
                occurred_at=now,
            )
        )
        return updated
