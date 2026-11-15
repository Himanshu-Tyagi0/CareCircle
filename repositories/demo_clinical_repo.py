"""Demo implementations of MedicationRepository and DoseRepository."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from core.errors import ConflictError, NotFoundError
from core.models import CantTakeReason, Criticality, DoseState
from core.models_clinical import (
    AuditEvent,
    DoseEvent,
    DoseInstance,
    Medication,
    MedicationSchedule,
    OutboxStatus,
)

from repositories.demo_store import DemoStore


class DemoMedicationRepository:
    """Thread-safe demo MedicationRepository preserving historical versions."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def list_medications(
        self, circle_id: UUID, *, include_archived: bool = False
    ) -> list[Medication]:
        with self._s.lock:
            return sorted(
                [
                    m
                    for m in self._s.medications.values()
                    if m.circle_id == circle_id and (include_archived or m.archived_at is None)
                ],
                key=lambda m: m.created_at,
            )

    def get_medication(self, circle_id: UUID, medication_id: UUID) -> Medication | None:
        with self._s.lock:
            med = self._s.medications.get(medication_id)
            return med if med and med.circle_id == circle_id else None

    def create_medication_with_schedule(
        self,
        *,
        circle_id: UUID,
        name: str,
        dose_text: str,
        instructions: str,
        start_date: date,
        end_date: date | None,
        criticality: Criticality,
        stock_count: float | None,
        stock_unit: str,
        refill_threshold: float | None,
        stock_units_per_dose: float | None,
        times_local: tuple[time, ...],
        weekdays: tuple[int, ...],
        timezone: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> tuple[Medication, MedicationSchedule]:
        with self._s.lock:
            med_id = uuid.uuid4()
            med = Medication(
                id=med_id,
                circle_id=circle_id,
                name=name,
                dose_text=dose_text,
                instructions=instructions,
                start_date=start_date,
                end_date=end_date,
                criticality=criticality,
                stock_count=stock_count,
                stock_unit=stock_unit,
                refill_threshold=refill_threshold,
                stock_units_per_dose=stock_units_per_dose,
                created_at=now,
                updated_at=now,
            )
            sched = MedicationSchedule(
                id=uuid.uuid4(),
                circle_id=circle_id,
                medication_id=med_id,
                version=1,
                times_local=times_local,
                weekdays=weekdays,
                start_date=start_date,
                end_date=end_date,
                timezone=timezone,
                active=True,
                created_at=now,
            )
            self._s.medications[med_id] = med
            self._s.schedules[sched.id] = sched
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="medication_created",
                    target_type="medication",
                    target_id=str(med_id),
                    metadata={"criticality": criticality.value},
                    occurred_at=now,
                )
            )
            return med, sched

    def update_medication_and_schedule(
        self,
        *,
        circle_id: UUID,
        medication_id: UUID,
        med_updates: dict[str, Any],
        new_schedule_times: tuple[time, ...] | None,
        new_weekdays: tuple[int, ...] | None,
        new_end_date: date | None,
        timezone: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> tuple[Medication, MedicationSchedule | None]:
        with self._s.lock:
            med = self.get_medication(circle_id, medication_id)
            if not med:
                raise NotFoundError("errors.not_found")
            updated_med = med.model_copy(
                update={**med_updates, "version": med.version + 1, "updated_at": now}
            )
            self._s.medications[medication_id] = updated_med

            new_sched: MedicationSchedule | None = None
            if new_schedule_times is not None or new_weekdays is not None:
                old_sched = self.get_active_schedule(circle_id, medication_id)
                next_ver = (old_sched.version + 1) if old_sched else 1
                if old_sched:
                    self._s.schedules[old_sched.id] = old_sched.model_copy(update={"active": False})
                new_sched = MedicationSchedule(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    medication_id=medication_id,
                    version=next_ver,
                    times_local=new_schedule_times
                    or (old_sched.times_local if old_sched else (time(8, 0),)),
                    weekdays=new_weekdays
                    or (old_sched.weekdays if old_sched else (0, 1, 2, 3, 4, 5, 6)),
                    start_date=updated_med.start_date,
                    end_date=new_end_date if new_end_date is not None else updated_med.end_date,
                    timezone=timezone,
                    active=True,
                    created_at=now,
                )
                self._s.schedules[new_sched.id] = new_sched

            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="medication_updated",
                    target_type="medication",
                    target_id=str(medication_id),
                    metadata={"version": updated_med.version},
                    occurred_at=now,
                )
            )
            return updated_med, new_sched

    def archive_medication(
        self, *, circle_id: UUID, medication_id: UUID, actor_account_id: UUID, now: datetime
    ) -> Medication:
        with self._s.lock:
            med = self.get_medication(circle_id, medication_id)
            if not med:
                raise NotFoundError("errors.not_found")
            archived = med.model_copy(update={"archived_at": now, "updated_at": now})
            self._s.medications[medication_id] = archived
            for sid, sched in list(self._s.schedules.items()):
                if sched.medication_id == medication_id and sched.active:
                    self._s.schedules[sid] = sched.model_copy(update={"active": False})
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="medication_archived",
                    target_type="medication",
                    target_id=str(medication_id),
                    occurred_at=now,
                )
            )
            return archived

    def adjust_stock(
        self,
        *,
        circle_id: UUID,
        medication_id: UUID,
        new_stock_count: float,
        reason: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> Medication:
        with self._s.lock:
            med = self.get_medication(circle_id, medication_id)
            if not med:
                raise NotFoundError("errors.not_found")
            reset_alert = (
                med.refill_threshold is not None and new_stock_count > med.refill_threshold
            )
            updated = med.model_copy(
                update={
                    "stock_count": new_stock_count,
                    "low_stock_alert_active": False if reset_alert else med.low_stock_alert_active,
                    "updated_at": now,
                }
            )
            self._s.medications[medication_id] = updated
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="stock_adjusted",
                    target_type="medication",
                    target_id=str(medication_id),
                    metadata={"reason": reason[:120], "new_stock": new_stock_count},
                    occurred_at=now,
                )
            )
            return updated

    def set_low_stock_alert_active(
        self, *, circle_id: UUID, medication_id: UUID, active: bool
    ) -> None:
        with self._s.lock:
            med = self.get_medication(circle_id, medication_id)
            if med:
                self._s.medications[medication_id] = med.model_copy(
                    update={"low_stock_alert_active": active}
                )

    def list_active_schedules(self, circle_id: UUID) -> list[MedicationSchedule]:
        with self._s.lock:
            return [s for s in self._s.schedules.values() if s.circle_id == circle_id and s.active]

    def get_active_schedule(
        self, circle_id: UUID, medication_id: UUID
    ) -> MedicationSchedule | None:
        with self._s.lock:
            for s in self._s.schedules.values():
                if s.circle_id == circle_id and s.medication_id == medication_id and s.active:
                    return s
            return None


class DemoDoseRepository:
    """Thread-safe demo DoseRepository enforcing double-tap and non-negative inventory."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def upsert_dose_instances(self, doses: list[DoseInstance]) -> int:
        with self._s.lock:
            inserted = 0
            for d in doses:
                key = (d.medication_id, d.due_at)
                if key not in self._s.dose_med_due_index:
                    self._s.doses[d.id] = d
                    self._s.dose_med_due_index[key] = d.id
                    inserted += 1
            return inserted

    def list_doses_in_window(
        self, circle_id: UUID, start_utc: datetime, end_utc: datetime
    ) -> list[DoseInstance]:
        with self._s.lock:
            return sorted(
                [
                    d
                    for d in self._s.doses.values()
                    if d.circle_id == circle_id and start_utc <= d.due_at <= end_utc
                ],
                key=lambda d: d.due_at,
            )

    def list_active_pending_doses(self, now: datetime, limit: int = 100) -> list[DoseInstance]:
        with self._s.lock:
            items = [
                d
                for d in self._s.doses.values()
                if d.circle_id not in self._s.deleted_circle_ids
                and d.state in (DoseState.PENDING, DoseState.SNOOZED, DoseState.CANT_TAKE)
                and d.due_at <= now
                and d.expires_at > now
            ]
            return sorted(items, key=lambda d: d.due_at)[:limit]

    def get_dose(self, circle_id: UUID, dose_id: UUID) -> DoseInstance | None:
        with self._s.lock:
            d = self._s.doses.get(dose_id)
            return d if d and d.circle_id == circle_id else None

    def get_dose_by_id_internal(self, dose_id: UUID) -> DoseInstance | None:
        with self._s.lock:
            return self._s.doses.get(dose_id)

    def list_dose_events(self, circle_id: UUID, dose_id: UUID) -> list[DoseEvent]:
        with self._s.lock:
            return [
                e for e in self._s.dose_events if e.circle_id == circle_id and e.dose_id == dose_id
            ]

    def _cancel_dose_escalation_outbox(self, dose_id: UUID, now: datetime) -> None:
        for oid, item in list(self._s.outbox.items()):
            if (
                item.resource_id == dose_id
                and item.resource_type == "dose"
                and item.event_type.startswith("dose_escalation_")
                and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
            ):
                self._s.outbox[oid] = item.model_copy(
                    update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                )

    def transition_taken(
        self, *, circle_id: UUID, dose_id: UUID, actor_account_id: UUID, now: datetime
    ) -> tuple[DoseInstance, bool, bool]:
        with self._s.lock:
            d = self.get_dose(circle_id, dose_id)
            if not d:
                raise NotFoundError("errors.not_found")
            if d.state == DoseState.TAKEN:
                return d, True, d.inventory_discrepancy
            if d.state in (DoseState.CANCELLED, DoseState.EXPIRED):
                raise ConflictError("errors.conflict")

            med = self._s.medications[d.medication_id]
            discrepancy = False
            decremented = False
            stock_delta = 0.0
            if (
                med.stock_count is not None
                and med.stock_units_per_dose is not None
                and not d.inventory_decremented
            ):
                if med.stock_count <= 0:
                    discrepancy = True
                else:
                    new_stock = max(0.0, med.stock_count - med.stock_units_per_dose)
                    stock_delta = new_stock - med.stock_count
                    self._s.medications[med.id] = med.model_copy(
                        update={"stock_count": new_stock, "updated_at": now}
                    )
                    decremented = True

            updated = d.model_copy(
                update={
                    "state": DoseState.TAKEN,
                    "confirmed_at": now,
                    "confirmed_by_account_id": actor_account_id,
                    "snoozed_until": None,
                    "inventory_decremented": d.inventory_decremented or decremented,
                    "inventory_discrepancy": d.inventory_discrepancy or discrepancy,
                    "updated_at": now,
                }
            )
            self._s.doses[dose_id] = updated
            self._cancel_dose_escalation_outbox(dose_id, now)
            self._s.dose_events.append(
                DoseEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    dose_id=dose_id,
                    event_type="confirmed_taken",
                    actor_account_id=actor_account_id,
                    previous_state=d.state,
                    new_state=DoseState.TAKEN,
                    stock_delta=stock_delta,
                    occurred_at=now,
                )
            )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="dose_taken",
                    target_type="dose",
                    target_id=str(dose_id),
                    metadata={"discrepancy": discrepancy},
                    occurred_at=now,
                )
            )
            return updated, False, discrepancy

    def transition_snooze(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        snoozed_until: datetime,
        now: datetime,
    ) -> DoseInstance:
        with self._s.lock:
            d = self.get_dose(circle_id, dose_id)
            if not d:
                raise NotFoundError("errors.not_found")
            if d.state not in (DoseState.PENDING, DoseState.SNOOZED):
                raise ConflictError("errors.conflict")
            updated = d.model_copy(
                update={
                    "state": DoseState.SNOOZED,
                    "snoozed_until": snoozed_until,
                    "updated_at": now,
                }
            )
            self._s.doses[dose_id] = updated
            self._s.dose_events.append(
                DoseEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    dose_id=dose_id,
                    event_type="snoozed",
                    actor_account_id=actor_account_id,
                    previous_state=d.state,
                    new_state=DoseState.SNOOZED,
                    occurred_at=now,
                )
            )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="dose_snoozed",
                    target_type="dose",
                    target_id=str(dose_id),
                    occurred_at=now,
                )
            )
            return updated

    def transition_cant_take(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        reason: str,
        note: str,
        report_id: UUID,
        now: datetime,
    ) -> DoseInstance:
        with self._s.lock:
            d = self.get_dose(circle_id, dose_id)
            if not d:
                raise NotFoundError("errors.not_found")
            if d.state in (DoseState.TAKEN, DoseState.CANCELLED, DoseState.EXPIRED):
                raise ConflictError("errors.conflict")
            effective_report_id = d.cant_take_report_id or report_id
            updated = d.model_copy(
                update={
                    "state": DoseState.CANT_TAKE,
                    "cant_take_report_id": effective_report_id,
                    "cant_take_reason": CantTakeReason(reason),
                    "cant_take_note": note,
                    "snoozed_until": None,
                    "updated_at": now,
                }
            )
            self._s.doses[dose_id] = updated
            self._cancel_dose_escalation_outbox(dose_id, now)
            self._s.dose_events.append(
                DoseEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    dose_id=dose_id,
                    event_type="cant_take",
                    actor_account_id=actor_account_id,
                    previous_state=d.state,
                    new_state=DoseState.CANT_TAKE,
                    reason=reason,
                    note=note,
                    occurred_at=now,
                )
            )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="dose_cant_take",
                    target_type="dose",
                    target_id=str(dose_id),
                    metadata={"reason": reason},
                    occurred_at=now,
                )
            )
            return updated

    def transition_skip(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        reason: str,
        now: datetime,
    ) -> DoseInstance:
        with self._s.lock:
            d = self.get_dose(circle_id, dose_id)
            if not d:
                raise NotFoundError("errors.not_found")
            if d.state in (DoseState.TAKEN, DoseState.CANCELLED):
                raise ConflictError("errors.conflict")
            updated = d.model_copy(
                update={
                    "state": DoseState.SKIPPED,
                    "skip_reason": reason,
                    "snoozed_until": None,
                    "updated_at": now,
                }
            )
            self._s.doses[dose_id] = updated
            self._cancel_dose_escalation_outbox(dose_id, now)
            self._s.dose_events.append(
                DoseEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    dose_id=dose_id,
                    event_type="skipped",
                    actor_account_id=actor_account_id,
                    previous_state=d.state,
                    new_state=DoseState.SKIPPED,
                    reason=reason,
                    occurred_at=now,
                )
            )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="dose_skipped",
                    target_type="dose",
                    target_id=str(dose_id),
                    occurred_at=now,
                )
            )
            return updated

    def correct_dose(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        new_state: DoseState,
        reason: str,
        now: datetime,
    ) -> DoseInstance:
        with self._s.lock:
            d = self.get_dose(circle_id, dose_id)
            if not d:
                raise NotFoundError("errors.not_found")
            med = self._s.medications[d.medication_id]
            stock_delta = 0.0
            inv_dec = d.inventory_decremented
            if med.stock_count is not None and med.stock_units_per_dose is not None:
                if d.state != DoseState.TAKEN and new_state == DoseState.TAKEN and not inv_dec:
                    if med.stock_count > 0:
                        new_stock = max(0.0, med.stock_count - med.stock_units_per_dose)
                        stock_delta = new_stock - med.stock_count
                        self._s.medications[med.id] = med.model_copy(
                            update={"stock_count": new_stock, "updated_at": now}
                        )
                        inv_dec = True
                elif d.state == DoseState.TAKEN and new_state != DoseState.TAKEN and inv_dec:
                    new_stock = med.stock_count + med.stock_units_per_dose
                    stock_delta = med.stock_units_per_dose
                    self._s.medications[med.id] = med.model_copy(
                        update={"stock_count": new_stock, "updated_at": now}
                    )
                    inv_dec = False

            updated = d.model_copy(
                update={
                    "state": new_state,
                    "confirmed_at": now if new_state == DoseState.TAKEN else None,
                    "confirmed_by_account_id": (
                        actor_account_id if new_state == DoseState.TAKEN else None
                    ),
                    "inventory_decremented": inv_dec,
                    "updated_at": now,
                }
            )
            self._s.doses[dose_id] = updated
            self._s.dose_events.append(
                DoseEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    dose_id=dose_id,
                    event_type="corrected",
                    actor_account_id=actor_account_id,
                    previous_state=d.state,
                    new_state=new_state,
                    reason=reason,
                    stock_delta=stock_delta,
                    occurred_at=now,
                )
            )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="dose_corrected",
                    target_type="dose",
                    target_id=str(dose_id),
                    metadata={"from": d.state.value, "to": new_state.value},
                    occurred_at=now,
                )
            )
            return updated

    def cancel_future_doses_for_medication(
        self, *, circle_id: UUID, medication_id: UUID, from_utc: datetime, now: datetime
    ) -> int:
        with self._s.lock:
            cnt = 0
            for did, d in list(self._s.doses.items()):
                if (
                    d.circle_id == circle_id
                    and d.medication_id == medication_id
                    and d.due_at >= from_utc
                    and d.state in (DoseState.PENDING, DoseState.SNOOZED)
                ):
                    self._s.doses[did] = d.model_copy(
                        update={"state": DoseState.CANCELLED, "updated_at": now}
                    )
                    self._cancel_dose_escalation_outbox(did, now)
                    cnt += 1
            return cnt

    def expire_overdue_doses(self, *, now: datetime) -> int:
        with self._s.lock:
            cnt = 0
            for did, d in list(self._s.doses.items()):
                if d.state in (DoseState.PENDING, DoseState.SNOOZED) and d.expires_at <= now:
                    self._s.doses[did] = d.model_copy(
                        update={"state": DoseState.EXPIRED, "updated_at": now}
                    )
                    self._cancel_dose_escalation_outbox(did, now)
                    cnt += 1
            return cnt

    def update_escalation_progress(
        self,
        *,
        dose_id: UUID,
        high_water: int,
        escalation_state: dict[str, Any],
        now: datetime,
    ) -> None:
        with self._s.lock:
            d = self._s.doses.get(dose_id)
            if d:
                self._s.doses[dose_id] = d.model_copy(
                    update={
                        "escalation_high_water": max(d.escalation_high_water, high_water),
                        "escalation_state": escalation_state,
                        "updated_at": now,
                    }
                )
