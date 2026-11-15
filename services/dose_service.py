"""Dose confirmation, double-dose guard, snooze, can't-take reporting, and corrections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from core.clock import Clock
from core.errors import NotFoundError, ValidationError
from core.models import ActorContext, CantTakeReason, DoseState
from core.models_clinical import DoseEvent, DoseInstance
from core.permissions import Action, authorize
from repositories.interfaces import CircleRepository, DoseRepository

from services.medication_service import MedicationService


@dataclass(frozen=True)
class DoseActionResult:
    """Result returned by dose logging actions."""

    dose: DoseInstance
    already_confirmed: bool = False
    inventory_discrepancy: bool = False
    delivery_status: str = ""


class DoseService:
    """Authorized dose state transitions and inventory coordination."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        dose_repo: DoseRepository,
        med_service: MedicationService,
        clock: Clock,
        notification_service: object | None = None,
    ) -> None:
        self._circle = circle_repo
        self._dose = dose_repo
        self._med_svc = med_service
        self._clock = clock
        self._notif = notification_service

    def list_doses_in_window(
        self, actor: ActorContext, *, start_utc: datetime, end_utc: datetime
    ) -> list[DoseInstance]:
        """List circle doses in UTC window after enforcing medications consent."""
        authorize(actor, Action.VIEW_MEDICATIONS, target_circle_id=actor.circle_id)
        return self._dose.list_doses_in_window(actor.circle_id, start_utc, end_utc)

    def list_dose_events(self, actor: ActorContext, *, dose_id: UUID) -> list[DoseEvent]:
        """List immutable dose events for a dose inside actor's circle."""
        authorize(actor, Action.VIEW_MEDICATIONS, target_circle_id=actor.circle_id)
        return self._dose.list_dose_events(actor.circle_id, dose_id)

    def mark_taken(self, actor: ActorContext, *, dose_id: UUID) -> DoseActionResult:
        """Confirm dose taken idempotently with single stock decrement."""
        authorize(actor, Action.LOG_DOSE, target_circle_id=actor.circle_id)
        now = self._clock.now()
        dose, already, discrepancy = self._dose.transition_taken(
            circle_id=actor.circle_id,
            dose_id=dose_id,
            actor_account_id=actor.account_id,
            now=now,
        )
        if not already:
            self._med_svc.check_and_queue_low_stock(actor.circle_id, dose.medication_id)
        return DoseActionResult(
            dose=dose, already_confirmed=already, inventory_discrepancy=discrepancy
        )

    def snooze_dose(
        self, actor: ActorContext, *, dose_id: UUID, minutes: int = 10
    ) -> DoseActionResult:
        """Snooze reminders while preserving original due_at for adherence and Care Chain."""
        authorize(actor, Action.LOG_DOSE, target_circle_id=actor.circle_id)
        if not (1 <= minutes <= 120):
            raise ValidationError("errors.validation")
        now = self._clock.now()
        dose = self._dose.transition_snooze(
            circle_id=actor.circle_id,
            dose_id=dose_id,
            actor_account_id=actor.account_id,
            snoozed_until=now + timedelta(minutes=minutes),
            now=now,
        )
        return DoseActionResult(dose=dose)

    def report_cant_take(
        self,
        actor: ActorContext,
        *,
        dose_id: UUID,
        reason: CantTakeReason,
        note: str = "",
    ) -> DoseActionResult:
        """Record inability to take a dose and immediately queue primary-contact event."""
        authorize(actor, Action.LOG_DOSE, target_circle_id=actor.circle_id)
        cleaned_note = note.strip()[:300]
        if reason == CantTakeReason.OTHER and not cleaned_note:
            raise ValidationError("errors.cant_take_note_required")

        now = self._clock.now()
        report_id = uuid.uuid4()
        dose = self._dose.transition_cant_take(
            circle_id=actor.circle_id,
            dose_id=dose_id,
            actor_account_id=actor.account_id,
            reason=reason.value,
            note=cleaned_note,
            report_id=report_id,
            now=now,
        )
        delivery_status = "queued"
        if self._notif is not None:
            circle = self._circle.get_circle(actor.circle_id)
            if circle:
                delivery_status = self._notif.queue_and_dispatch_cant_take(circle=circle, dose=dose)
        return DoseActionResult(dose=dose, delivery_status=delivery_status)

    def skip_dose(self, actor: ActorContext, *, dose_id: UUID, reason: str) -> DoseActionResult:
        """Explicitly skip a dose with a required reason, distinguishing from unconfirmed."""
        authorize(actor, Action.LOG_DOSE, target_circle_id=actor.circle_id)
        cleaned = reason.strip()[:300]
        if not cleaned:
            raise ValidationError("errors.skip_reason_required")
        dose = self._dose.transition_skip(
            circle_id=actor.circle_id,
            dose_id=dose_id,
            actor_account_id=actor.account_id,
            reason=cleaned,
            now=self._clock.now(),
        )
        return DoseActionResult(dose=dose)

    def correct_dose(
        self,
        actor: ActorContext,
        *,
        dose_id: UUID,
        new_state: DoseState,
        reason: str,
    ) -> DoseActionResult:
        """Authorized historical dose correction with required audit reason and stock adjustment."""
        authorize(actor, Action.CORRECT_DOSE, target_circle_id=actor.circle_id)
        cleaned = reason.strip()[:300]
        if not cleaned:
            raise ValidationError("errors.correction_reason_required")
        existing = self._dose.get_dose(actor.circle_id, dose_id)
        if not existing:
            raise NotFoundError("errors.not_found")
        updated = self._dose.correct_dose(
            circle_id=actor.circle_id,
            dose_id=dose_id,
            actor_account_id=actor.account_id,
            new_state=new_state,
            reason=cleaned,
            now=self._clock.now(),
        )
        return DoseActionResult(dose=updated)
