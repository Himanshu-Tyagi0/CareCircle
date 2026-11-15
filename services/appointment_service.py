"""Appointment CRUD and versioned 24h / 1h reminder evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from core.clock import Clock
from core.errors import NotFoundError, ValidationError
from core.models import ActorContext
from core.models_clinical import Appointment
from core.permissions import Action, authorize
from repositories.interfaces import CollabRepository

from services.auth_service import validate_timezone


def select_appointment_reminder_level(*, starts_at: datetime, now: datetime) -> int | None:
    """Return only the currently relevant reminder window (24h or 1h) to avoid late-cron bursts."""
    if now >= starts_at:
        return None
    until = starts_at - now
    if until <= timedelta(hours=1):
        return 2  # 1-hour reminder supersedes 24-hour reminder on late cron runs
    if until <= timedelta(hours=24):
        return 1  # 24-hour reminder
    return None


class AppointmentService:
    """Authorized appointment CRUD with versioned reminder invalidation."""

    def __init__(self, *, collab_repo: CollabRepository, clock: Clock) -> None:
        self._collab = collab_repo
        self._clock = clock

    def list_appointments(
        self, actor: ActorContext, *, include_cancelled: bool = False
    ) -> list[Appointment]:
        """List circle appointments after enforcing APPOINTMENTS consent."""
        authorize(actor, Action.VIEW_APPOINTMENTS, target_circle_id=actor.circle_id)
        return self._collab.list_appointments(actor.circle_id, include_cancelled=include_cancelled)

    def create_appointment(
        self,
        actor: ActorContext,
        *,
        doctor_name: str,
        specialty: str = "",
        location: str = "",
        starts_at: datetime,
        timezone: str | None = None,
        notes: str = "",
    ) -> Appointment:
        """Create and audit a medical appointment."""
        authorize(actor, Action.MANAGE_APPOINTMENTS, target_circle_id=actor.circle_id)
        clean_doc = doctor_name.strip()
        if not clean_doc or len(clean_doc) > 120:
            raise ValidationError("errors.validation")
        tz = validate_timezone(timezone or actor.circle_timezone)
        return self._collab.create_appointment(
            circle_id=actor.circle_id,
            doctor_name=clean_doc,
            specialty=specialty.strip()[:120],
            location=location.strip()[:200],
            starts_at=starts_at,
            timezone=tz,
            notes=notes.strip()[:500],
            actor_account_id=actor.account_id,
            now=self._clock.now(),
        )

    def update_appointment(
        self,
        actor: ActorContext,
        *,
        appointment_id: UUID,
        doctor_name: str | None = None,
        specialty: str | None = None,
        location: str | None = None,
        starts_at: datetime | None = None,
        notes: str | None = None,
    ) -> Appointment:
        """Update an appointment, bump version, and cancel obsolete pending reminders."""
        authorize(actor, Action.MANAGE_APPOINTMENTS, target_circle_id=actor.circle_id)
        existing = self._collab.get_appointment(actor.circle_id, appointment_id)
        if not existing:
            raise NotFoundError("errors.not_found")
        updates: dict[str, object] = {}
        if doctor_name is not None:
            cleaned = doctor_name.strip()
            if not cleaned or len(cleaned) > 120:
                raise ValidationError("errors.validation")
            updates["doctor_name"] = cleaned
        if specialty is not None:
            updates["specialty"] = specialty.strip()[:120]
        if location is not None:
            updates["location"] = location.strip()[:200]
        if starts_at is not None:
            updates["starts_at"] = starts_at
        if notes is not None:
            updates["notes"] = notes.strip()[:500]
        return self._collab.update_appointment(
            circle_id=actor.circle_id,
            appointment_id=appointment_id,
            updates=updates,
            actor_account_id=actor.account_id,
            now=self._clock.now(),
        )

    def cancel_appointment(self, actor: ActorContext, *, appointment_id: UUID) -> Appointment:
        """Cancel an appointment and invalidate pending reminders."""
        authorize(actor, Action.MANAGE_APPOINTMENTS, target_circle_id=actor.circle_id)
        return self._collab.cancel_appointment(
            circle_id=actor.circle_id,
            appointment_id=appointment_id,
            actor_account_id=actor.account_id,
            now=self._clock.now(),
        )
