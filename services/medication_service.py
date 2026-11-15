"""Medication CRUD, inventory validation, refill forecast, and low-stock episode alerts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from core.clock import Clock
from core.errors import NotFoundError, ValidationError
from core.models import ActorContext, Criticality
from core.models_clinical import Medication, MedicationSchedule
from core.permissions import Action, authorize
from repositories.interfaces import CircleRepository, DoseRepository, MedicationRepository

from services.schedule_service import expand_schedule


@dataclass(frozen=True)
class RefillForecast:
    """Honest refill forecast or explicit unavailable status."""

    available: bool
    days_remaining: int | None = None
    estimated_runout_date: date | None = None
    reason_key: str = ""


def compute_refill_forecast(
    *,
    medication: Medication,
    schedule: MedicationSchedule | None,
    today_local: date,
) -> RefillForecast:
    """Compute refill days from explicit stock_count, units_per_dose, weekdays, and end_date."""
    if (
        medication.stock_count is None
        or medication.stock_units_per_dose is None
        or medication.stock_units_per_dose <= 0
        or schedule is None
        or not schedule.times_local
        or not schedule.weekdays
    ):
        return RefillForecast(available=False, reason_key="medications.refill_unavailable")

    remaining = float(medication.stock_count)
    if remaining <= 0:
        return RefillForecast(available=True, days_remaining=0, estimated_runout_date=today_local)

    per_dose = float(medication.stock_units_per_dose)
    doses_per_active_day = len(schedule.times_local)
    cur = max(today_local, schedule.start_date)
    end_limit = schedule.end_date or medication.end_date

    days_checked = 0
    while days_checked < 3650:
        if end_limit is not None and cur > end_limit:
            return RefillForecast(
                available=True,
                days_remaining=(cur - today_local).days,
                estimated_runout_date=end_limit,
            )
        if cur.weekday() in schedule.weekdays:
            needed = doses_per_active_day * per_dose
            if remaining < needed:
                return RefillForecast(
                    available=True,
                    days_remaining=(cur - today_local).days,
                    estimated_runout_date=cur,
                )
            remaining -= needed
        cur += timedelta(days=1)
        days_checked += 1

    return RefillForecast(
        available=True,
        days_remaining=3650,
        estimated_runout_date=today_local + timedelta(days=3650),
    )


def _validate_finite_nonneg(val: float | None, *, positive: bool = False) -> float | None:
    if val is None:
        return None
    if not math.isfinite(val):
        raise ValidationError("errors.validation")
    if positive and val <= 0:
        raise ValidationError("errors.validation")
    if not positive and val < 0:
        raise ValidationError("errors.validation")
    return float(val)


def parse_times_input(raw_times: list[time] | tuple[time, ...]) -> tuple[time, ...]:
    """Validate non-empty bounded unique daily times."""
    cleaned = tuple(sorted(set(raw_times)))
    if not cleaned or len(cleaned) > 12:
        raise ValidationError("errors.validation")
    return cleaned


def parse_weekdays_input(weekdays: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    """Validate weekdays in 0..6."""
    cleaned = tuple(sorted(set(weekdays)))
    if not cleaned or any(d < 0 or d > 6 for d in cleaned):
        raise ValidationError("errors.validation")
    return cleaned


class MedicationService:
    """Authorized medication CRUD, versioned schedules, and inventory adjustments."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        med_repo: MedicationRepository,
        dose_repo: DoseRepository,
        clock: Clock,
        notification_service: object | None = None,
    ) -> None:
        self._circle = circle_repo
        self._med = med_repo
        self._dose = dose_repo
        self._clock = clock
        self._notif = notification_service

    def list_medications(
        self, actor: ActorContext, *, include_archived: bool = False
    ) -> list[tuple[Medication, MedicationSchedule | None, RefillForecast]]:
        """List circle medications with schedules and honest refill forecasts."""
        authorize(actor, Action.VIEW_MEDICATIONS, target_circle_id=actor.circle_id)
        meds = self._med.list_medications(actor.circle_id, include_archived=include_archived)
        scheds = {s.medication_id: s for s in self._med.list_active_schedules(actor.circle_id)}
        today_local = self._clock.now().astimezone(ZoneInfo(actor.circle_timezone)).date()
        return [
            (
                m,
                scheds.get(m.id),
                compute_refill_forecast(
                    medication=m, schedule=scheds.get(m.id), today_local=today_local
                ),
            )
            for m in meds
        ]

    def create_medication(
        self,
        actor: ActorContext,
        *,
        name: str,
        dose_text: str,
        instructions: str = "",
        start_date: date,
        end_date: date | None = None,
        criticality: Criticality = Criticality.NORMAL,
        stock_count: float | None = None,
        stock_unit: str = "",
        refill_threshold: float | None = None,
        stock_units_per_dose: float | None = None,
        times_local: tuple[time, ...] | list[time],
        weekdays: tuple[int, ...] | list[int] = (0, 1, 2, 3, 4, 5, 6),
    ) -> tuple[Medication, MedicationSchedule]:
        """Validate, create medication + version 1 schedule, and generate initial 48h horizon."""
        authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
        clean_name = name.strip()
        clean_dose = dose_text.strip()
        if not clean_name or len(clean_name) > 120 or not clean_dose or len(clean_dose) > 120:
            raise ValidationError("errors.validation")
        if end_date is not None and end_date < start_date:
            raise ValidationError("errors.validation")
        clean_stock = _validate_finite_nonneg(stock_count)
        clean_thresh = _validate_finite_nonneg(refill_threshold)
        clean_upd = _validate_finite_nonneg(stock_units_per_dose, positive=True)
        clean_times = parse_times_input(times_local)
        clean_days = parse_weekdays_input(weekdays)

        existing = self._med.list_medications(actor.circle_id)
        if len(existing) >= 100:
            raise ValidationError("errors.validation")

        now = self._clock.now()
        med, sched = self._med.create_medication_with_schedule(
            circle_id=actor.circle_id,
            name=clean_name,
            dose_text=clean_dose,
            instructions=instructions.strip()[:400],
            start_date=start_date,
            end_date=end_date,
            criticality=criticality,
            stock_count=clean_stock,
            stock_unit=stock_unit.strip()[:40],
            refill_threshold=clean_thresh,
            stock_units_per_dose=clean_upd,
            times_local=clean_times,
            weekdays=clean_days,
            timezone=actor.circle_timezone,
            actor_account_id=actor.account_id,
            now=now,
        )
        doses = expand_schedule(
            schedule=sched,
            medication=med,
            window_start_utc=now - timedelta(hours=12),
            window_end_utc=now + timedelta(hours=48),
            now=now,
        )
        if doses:
            self._dose.upsert_dose_instances(doses)
        self.check_and_queue_low_stock(actor.circle_id, med.id)
        return med, sched

    def update_medication_schedule(
        self,
        actor: ActorContext,
        *,
        medication_id: UUID,
        name: str | None = None,
        dose_text: str | None = None,
        instructions: str | None = None,
        criticality: Criticality | None = None,
        end_date: date | None = None,
        times_local: tuple[time, ...] | list[time] | None = None,
        weekdays: tuple[int, ...] | list[int] | None = None,
    ) -> tuple[Medication, MedicationSchedule | None]:
        """Update medication/future schedule version without altering past confirmations."""
        authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
        existing = self._med.get_medication(actor.circle_id, medication_id)
        if not existing:
            raise NotFoundError("errors.not_found")

        updates: dict[str, object] = {}
        if name is not None:
            cleaned = name.strip()
            if not cleaned or len(cleaned) > 120:
                raise ValidationError("errors.validation")
            updates["name"] = cleaned
        if dose_text is not None:
            cleaned = dose_text.strip()
            if not cleaned or len(cleaned) > 120:
                raise ValidationError("errors.validation")
            updates["dose_text"] = cleaned
        if instructions is not None:
            updates["instructions"] = instructions.strip()[:400]
        if criticality is not None:
            updates["criticality"] = criticality
        if end_date is not None:
            if end_date < existing.start_date:
                raise ValidationError("errors.validation")
            updates["end_date"] = end_date

        clean_times = parse_times_input(times_local) if times_local is not None else None
        clean_days = parse_weekdays_input(weekdays) if weekdays is not None else None

        now = self._clock.now()
        updated_med, new_sched = self._med.update_medication_and_schedule(
            circle_id=actor.circle_id,
            medication_id=medication_id,
            med_updates=updates,
            new_schedule_times=clean_times,
            new_weekdays=clean_days,
            new_end_date=end_date,
            timezone=actor.circle_timezone,
            actor_account_id=actor.account_id,
            now=now,
        )
        if new_sched is not None:
            self._dose.cancel_future_doses_for_medication(
                circle_id=actor.circle_id,
                medication_id=medication_id,
                from_utc=now,
                now=now,
            )
            doses = expand_schedule(
                schedule=new_sched,
                medication=updated_med,
                window_start_utc=now,
                window_end_utc=now + timedelta(hours=48),
                now=now,
            )
            if doses:
                self._dose.upsert_dose_instances(doses)
        return updated_med, new_sched

    def archive_medication(self, actor: ActorContext, *, medication_id: UUID) -> Medication:
        """Archive a medication while preserving historical doses and cancel future pending ones."""
        authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
        now = self._clock.now()
        med = self._med.archive_medication(
            circle_id=actor.circle_id,
            medication_id=medication_id,
            actor_account_id=actor.account_id,
            now=now,
        )
        self._dose.cancel_future_doses_for_medication(
            circle_id=actor.circle_id,
            medication_id=medication_id,
            from_utc=now,
            now=now,
        )
        return med

    def adjust_stock(
        self,
        actor: ActorContext,
        *,
        medication_id: UUID,
        new_stock_count: float,
        reason: str = "restock",
    ) -> Medication:
        """Adjust stock nonnegatively and reset low-stock episode after meaningful restock."""
        authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
        clean_stock = _validate_finite_nonneg(new_stock_count)
        if clean_stock is None:
            raise ValidationError("errors.validation")
        updated = self._med.adjust_stock(
            circle_id=actor.circle_id,
            medication_id=medication_id,
            new_stock_count=clean_stock,
            reason=reason.strip()[:120] or "restock",
            actor_account_id=actor.account_id,
            now=self._clock.now(),
        )
        self.check_and_queue_low_stock(actor.circle_id, medication_id)
        return updated

    def check_and_queue_low_stock(self, circle_id: UUID, medication_id: UUID) -> bool:
        """Deduplicate low-stock alerts per episode and respect recipient category consent."""
        med = self._med.get_medication(circle_id, medication_id)
        if (
            not med
            or med.archived_at is not None
            or med.stock_count is None
            or med.refill_threshold is None
        ):
            return False

        if med.stock_count > med.refill_threshold:
            if med.low_stock_alert_active:
                self._med.set_low_stock_alert_active(
                    circle_id=circle_id, medication_id=medication_id, active=False
                )
            return False

        if med.low_stock_alert_active:
            return False

        self._med.set_low_stock_alert_active(
            circle_id=circle_id, medication_id=medication_id, active=True
        )
        if self._notif is not None:
            circle = self._circle.get_circle(circle_id)
            if circle:
                self._notif.queue_low_stock_alert(circle=circle, medication=med)
        return True
