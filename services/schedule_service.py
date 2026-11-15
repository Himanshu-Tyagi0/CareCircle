"""DST-aware schedule expansion, 48h rolling generation, and downtime gap reconciliation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.clock import Clock
from core.models import DoseState
from core.models_clinical import DoseInstance, Medication, MedicationSchedule
from repositories.interfaces import CircleRepository, DoseRepository, MedicationRepository


def resolve_local_instant(local_d: date, local_t: time, tz: ZoneInfo) -> tuple[datetime, bool, str]:
    """Convert local date/time to UTC handling DST gaps (shift forward) and folds (fold=0)."""
    naive = datetime.combine(local_d, local_t)
    dt0 = naive.replace(tzinfo=tz, fold=0)
    utc0 = dt0.astimezone(UTC)
    roundtrip = utc0.astimezone(tz)

    # Nonexistent local time (spring-forward gap): roundtrip local wall time differs
    if roundtrip.date() != local_d or roundtrip.time() != local_t:
        # Advance minute-by-minute until the local wall-clock instant exists cleanly
        probe = utc0
        for _ in range(180):
            cand_local = probe.astimezone(tz)
            if cand_local.replace(tzinfo=None) >= naive:
                # Verify this instant roundtrips cleanly
                recheck = cand_local.replace(tzinfo=tz, fold=0).astimezone(UTC)
                if recheck == probe:
                    return (
                        probe,
                        True,
                        f"shifted_from_{local_t.strftime('%H:%M')}_to_{cand_local.strftime('%H:%M')}",
                    )
            probe += timedelta(minutes=1)
        return utc0, True, f"shifted_from_{local_t.strftime('%H:%M')}"

    # Repeated local time (fall-back fold): fold=0 chooses first occurrence deterministically
    return utc0, False, ""


def expand_schedule(
    *,
    schedule: MedicationSchedule,
    medication: Medication,
    window_start_utc: datetime,
    window_end_utc: datetime,
    now: datetime,
) -> list[DoseInstance]:
    """Pure schedule expansion generating deduplicated DST-aware DoseInstances."""
    if not schedule.active or medication.archived_at is not None:
        return []

    tz = ZoneInfo(schedule.timezone)
    local_start = (window_start_utc.astimezone(tz).date()) - timedelta(days=1)
    local_end = (window_end_utc.astimezone(tz).date()) + timedelta(days=2)

    if local_start < schedule.start_date:
        local_start = schedule.start_date
    effective_end = schedule.end_date or medication.end_date
    if effective_end is not None and local_end > effective_end:
        local_end = effective_end

    if local_end < local_start:
        return []

    raw_occurrences: list[tuple[datetime, date, time, bool, str]] = []
    seen_due: set[datetime] = set()
    cur = local_start
    while cur <= local_end:
        if cur.weekday() in schedule.weekdays:
            for t_local in sorted(schedule.times_local):
                due_utc, adjusted, note = resolve_local_instant(cur, t_local, tz)
                if due_utc not in seen_due:
                    seen_due.add(due_utc)
                    raw_occurrences.append((due_utc, cur, t_local, adjusted, note))
        cur += timedelta(days=1)

    raw_occurrences.sort(key=lambda item: item[0])

    instances: list[DoseInstance] = []
    for idx, (due_utc, loc_date, t_local, adjusted, note) in enumerate(raw_occurrences):
        if not (window_start_utc <= due_utc <= window_end_utc):
            continue
        twelve_hours = due_utc + timedelta(hours=12)
        if idx + 1 < len(raw_occurrences):
            expires_at = min(raw_occurrences[idx + 1][0], twelve_hours)
        else:
            expires_at = twelve_hours
        initial_state = DoseState.EXPIRED if expires_at <= now else DoseState.PENDING
        instances.append(
            DoseInstance(
                id=uuid.uuid4(),
                circle_id=medication.circle_id,
                medication_id=medication.id,
                schedule_id=schedule.id,
                schedule_version=schedule.version,
                medication_name=medication.name,
                dose_text=medication.dose_text,
                instructions=medication.instructions,
                criticality=medication.criticality,
                due_at=due_utc,
                local_date=loc_date,
                local_time_intended=t_local,
                dst_adjusted=adjusted,
                dst_adjustment_note=note,
                expires_at=expires_at,
                state=initial_state,
                created_at=now,
                updated_at=now,
            )
        )
    return instances


class ScheduleService:
    """Idempotent rolling 48-hour generation and downtime reconciliation service."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        med_repo: MedicationRepository,
        dose_repo: DoseRepository,
        clock: Clock,
    ) -> None:
        self._circle = circle_repo
        self._med = med_repo
        self._dose = dose_repo
        self._clock = clock

    def generate_horizon_for_circle(
        self,
        circle_id: uuid.UUID,
        *,
        horizon_hours: int = 48,
        backfill_days: int = 2,
    ) -> int:
        """Idempotently generate dose instances for active schedules in a bounded window."""
        now = self._clock.now()
        window_start = now - timedelta(days=backfill_days)
        window_end = now + timedelta(hours=horizon_hours)

        meds = {m.id: m for m in self._med.list_medications(circle_id)}
        schedules = self._med.list_active_schedules(circle_id)
        to_insert: list[DoseInstance] = []
        for sched in schedules:
            med = meds.get(sched.medication_id)
            if not med:
                continue
            to_insert.extend(
                expand_schedule(
                    schedule=sched,
                    medication=med,
                    window_start_utc=window_start,
                    window_end_utc=window_end,
                    now=now,
                )
            )
        if not to_insert:
            return 0
        return self._dose.upsert_dose_instances(to_insert)

    def reconcile_all_circles(self, *, max_days_chunk: int = 7) -> int:
        """Reconcile rolling 48h horizons across all active circles in bounded chunks."""
        total = 0
        for circle in self._circle.list_active_circles():
            total += self.generate_horizon_for_circle(
                circle.id, horizon_hours=48, backfill_days=min(max_days_chunk, 7)
            )
        return total
