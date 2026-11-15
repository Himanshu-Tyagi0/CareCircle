"""Demo implementation of CollabRepository for appointments, check-ins, acks, notes, and briefs."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from uuid import UUID

from core.errors import ConflictError, NotFoundError
from core.models import CheckInMood
from core.models_clinical import (
    Appointment,
    AuditEvent,
    CareAcknowledgement,
    CareNote,
    CheckInInstance,
    DoctorBriefSnapshot,
    OutboxStatus,
)

from repositories.demo_store import DemoStore


class DemoCollabRepository:
    """Thread-safe demo CollabRepository enforcing uniqueness and audit trail invariants."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def list_appointments(
        self, circle_id: UUID, *, include_cancelled: bool = False
    ) -> list[Appointment]:
        with self._s.lock:
            return sorted(
                [
                    a
                    for a in self._s.appointments.values()
                    if a.circle_id == circle_id and (include_cancelled or a.cancelled_at is None)
                ],
                key=lambda a: a.starts_at,
            )

    def list_upcoming_appointments(self, from_utc: datetime, to_utc: datetime) -> list[Appointment]:
        with self._s.lock:
            return [
                a
                for a in self._s.appointments.values()
                if a.circle_id not in self._s.deleted_circle_ids
                and a.cancelled_at is None
                and from_utc <= a.starts_at <= to_utc
            ]

    def get_appointment(self, circle_id: UUID, appointment_id: UUID) -> Appointment | None:
        with self._s.lock:
            a = self._s.appointments.get(appointment_id)
            return a if a and a.circle_id == circle_id else None

    def create_appointment(
        self,
        *,
        circle_id: UUID,
        doctor_name: str,
        specialty: str,
        location: str,
        starts_at: datetime,
        timezone: str,
        notes: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> Appointment:
        with self._s.lock:
            appt = Appointment(
                id=uuid.uuid4(),
                circle_id=circle_id,
                doctor_name=doctor_name,
                specialty=specialty,
                location=location,
                starts_at=starts_at,
                timezone=timezone,
                notes=notes,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._s.appointments[appt.id] = appt
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="appointment_created",
                    target_type="appointment",
                    target_id=str(appt.id),
                    occurred_at=now,
                )
            )
            return appt

    def update_appointment(
        self,
        *,
        circle_id: UUID,
        appointment_id: UUID,
        updates: dict[str, Any],
        actor_account_id: UUID,
        now: datetime,
    ) -> Appointment:
        with self._s.lock:
            appt = self.get_appointment(circle_id, appointment_id)
            if not appt or appt.cancelled_at is not None:
                raise NotFoundError("errors.not_found")
            updated = appt.model_copy(
                update={**updates, "version": appt.version + 1, "updated_at": now}
            )
            self._s.appointments[appointment_id] = updated
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.resource_id == appointment_id
                    and item.resource_type == "appointment"
                    and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                    )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="appointment_updated",
                    target_type="appointment",
                    target_id=str(appointment_id),
                    metadata={"version": updated.version},
                    occurred_at=now,
                )
            )
            return updated

    def cancel_appointment(
        self, *, circle_id: UUID, appointment_id: UUID, actor_account_id: UUID, now: datetime
    ) -> Appointment:
        with self._s.lock:
            appt = self.get_appointment(circle_id, appointment_id)
            if not appt:
                raise NotFoundError("errors.not_found")
            updated = appt.model_copy(
                update={"cancelled_at": now, "version": appt.version + 1, "updated_at": now}
            )
            self._s.appointments[appointment_id] = updated
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.resource_id == appointment_id
                    and item.resource_type == "appointment"
                    and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                    )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="appointment_cancelled",
                    target_type="appointment",
                    target_id=str(appointment_id),
                    occurred_at=now,
                )
            )
            return updated

    def get_or_create_checkin(
        self,
        *,
        circle_id: UUID,
        local_date: date,
        due_at: datetime,
        window_ends_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> CheckInInstance:
        with self._s.lock:
            key = (circle_id, local_date)
            existing_id = self._s.checkin_date_index.get(key)
            if existing_id:
                return self._s.checkins[existing_id]
            ci = CheckInInstance(
                id=uuid.uuid4(),
                circle_id=circle_id,
                local_date=local_date,
                due_at=due_at,
                window_ends_at=window_ends_at,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            self._s.checkins[ci.id] = ci
            self._s.checkin_date_index[key] = ci.id
            return ci

    def get_checkin_by_id(self, checkin_id: UUID) -> CheckInInstance | None:
        with self._s.lock:
            return self._s.checkins.get(checkin_id)

    def list_checkins(
        self, circle_id: UUID, start_date: date, end_date: date
    ) -> list[CheckInInstance]:
        with self._s.lock:
            return sorted(
                [
                    c
                    for c in self._s.checkins.values()
                    if c.circle_id == circle_id and start_date <= c.local_date <= end_date
                ],
                key=lambda c: c.local_date,
            )

    def list_active_unresponded_checkins(self, now: datetime) -> list[CheckInInstance]:
        with self._s.lock:
            return [
                c
                for c in self._s.checkins.values()
                if c.circle_id not in self._s.deleted_circle_ids
                and c.responded_at is None
                and c.due_at <= now
                and c.expires_at > now
            ]

    def submit_checkin_response(
        self,
        *,
        circle_id: UUID,
        checkin_id: UUID,
        mood: CheckInMood,
        note: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> tuple[CheckInInstance, CheckInMood | None]:
        with self._s.lock:
            ci = self._s.checkins.get(checkin_id)
            if not ci or ci.circle_id != circle_id:
                raise NotFoundError("errors.not_found")
            prev_mood = ci.mood
            updated = ci.model_copy(
                update={
                    "mood": mood,
                    "note": note,
                    "responded_at": now,
                    "responded_by_account_id": actor_account_id,
                    "updated_at": now,
                }
            )
            self._s.checkins[checkin_id] = updated
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.resource_id == checkin_id
                    and item.resource_type == "checkin"
                    and item.event_type.startswith("checkin_unanswered_")
                    and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                    )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="checkin_submitted" if prev_mood is None else "checkin_corrected",
                    target_type="checkin",
                    target_id=str(checkin_id),
                    metadata={"mood": mood.value},
                    occurred_at=now,
                )
            )
            return updated, prev_mood

    def update_checkin_escalation(
        self,
        *,
        checkin_id: UUID,
        high_water: int,
        escalation_state: dict[str, Any],
        now: datetime,
    ) -> None:
        with self._s.lock:
            ci = self._s.checkins.get(checkin_id)
            if ci:
                self._s.checkins[checkin_id] = ci.model_copy(
                    update={
                        "escalation_high_water": max(ci.escalation_high_water, high_water),
                        "escalation_state": escalation_state,
                        "updated_at": now,
                    }
                )

    def record_acknowledgement(
        self,
        *,
        circle_id: UUID,
        episode_type: str,
        resource_id: UUID,
        membership_id: UUID,
        actor_name: str,
        now: datetime,
    ) -> tuple[CareAcknowledgement, bool]:
        with self._s.lock:
            key = (episode_type, resource_id)
            if key in self._s.acknowledgements:
                return self._s.acknowledgements[key], False
            ack = CareAcknowledgement(
                id=uuid.uuid4(),
                circle_id=circle_id,
                episode_type=episode_type,
                resource_id=resource_id,
                acknowledged_by_membership_id=membership_id,
                acknowledged_by_name=actor_name,
                acknowledged_at=now,
            )
            self._s.acknowledgements[key] = ack
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.circle_id == circle_id
                    and item.resource_id == resource_id
                    and "_escalation_" in item.event_type
                    and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                    )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_membership_id=membership_id,
                    operation="care_acknowledged",
                    target_type=episode_type,
                    target_id=str(resource_id),
                    occurred_at=now,
                )
            )
            return ack, True

    def get_acknowledgement(
        self, *, episode_type: str, resource_id: UUID
    ) -> CareAcknowledgement | None:
        with self._s.lock:
            return self._s.acknowledgements.get((episode_type, resource_id))

    def list_acknowledgements(self, circle_id: UUID, limit: int = 20) -> list[CareAcknowledgement]:
        with self._s.lock:
            items = [a for a in self._s.acknowledgements.values() if a.circle_id == circle_id]
            return sorted(items, key=lambda a: a.acknowledged_at, reverse=True)[:limit]

    def list_notes(self, circle_id: UUID) -> list[CareNote]:
        with self._s.lock:
            return sorted(
                [n for n in self._s.notes.values() if n.circle_id == circle_id],
                key=lambda n: n.created_at,
                reverse=True,
            )

    def get_note(self, circle_id: UUID, note_id: UUID) -> CareNote | None:
        with self._s.lock:
            n = self._s.notes.get(note_id)
            return n if n and n.circle_id == circle_id else None

    def create_note(
        self,
        *,
        circle_id: UUID,
        author_account_id: UUID,
        author_name: str,
        note_type: str,
        content: str,
        now: datetime,
    ) -> CareNote:
        with self._s.lock:
            note = CareNote(
                id=uuid.uuid4(),
                circle_id=circle_id,
                author_account_id=author_account_id,
                author_name=author_name,
                note_type=note_type,
                content=content,
                created_at=now,
                updated_at=now,
            )
            self._s.notes[note.id] = note
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=author_account_id,
                    operation="note_created",
                    target_type=note_type,
                    target_id=str(note.id),
                    occurred_at=now,
                )
            )
            return note

    def update_note(
        self, *, circle_id: UUID, note_id: UUID, content: str, now: datetime
    ) -> CareNote:
        with self._s.lock:
            n = self.get_note(circle_id, note_id)
            if not n:
                raise NotFoundError("errors.not_found")
            updated = n.model_copy(update={"content": content, "updated_at": now})
            self._s.notes[note_id] = updated
            return updated

    def delete_note(self, *, circle_id: UUID, note_id: UUID) -> None:
        with self._s.lock:
            n = self.get_note(circle_id, note_id)
            if not n:
                raise NotFoundError("errors.not_found")
            del self._s.notes[note_id]

    def save_brief(self, brief: DoctorBriefSnapshot) -> DoctorBriefSnapshot:
        with self._s.lock:
            self._s.briefs[brief.id] = brief
            return brief

    def get_brief(self, circle_id: UUID, brief_id: UUID) -> DoctorBriefSnapshot | None:
        with self._s.lock:
            b = self._s.briefs.get(brief_id)
            return b if b and b.circle_id == circle_id else None

    def list_briefs(self, circle_id: UUID) -> list[DoctorBriefSnapshot]:
        with self._s.lock:
            return sorted(
                [b for b in self._s.briefs.values() if b.circle_id == circle_id],
                key=lambda b: b.created_at,
                reverse=True,
            )

    def approve_brief(
        self,
        *,
        circle_id: UUID,
        brief_id: UUID,
        expected_content_hash: str,
        recipient_membership_ids: tuple[UUID, ...],
        now: datetime,
    ) -> DoctorBriefSnapshot:
        with self._s.lock:
            b = self.get_brief(circle_id, brief_id)
            if not b:
                raise NotFoundError("errors.not_found")
            if b.content_hash != expected_content_hash:
                raise ConflictError("errors.conflict")
            updated = b.model_copy(
                update={
                    "approved_by_elder_at": now,
                    "approved_recipient_membership_ids": recipient_membership_ids,
                }
            )
            self._s.briefs[brief_id] = updated
            return updated

    def invalidate_brief_approvals(self, *, circle_id: UUID) -> None:
        with self._s.lock:
            for bid, b in list(self._s.briefs.items()):
                if b.circle_id == circle_id and b.approved_by_elder_at is not None:
                    self._s.briefs[bid] = b.model_copy(
                        update={
                            "approved_by_elder_at": None,
                            "approved_recipient_membership_ids": (),
                        }
                    )
