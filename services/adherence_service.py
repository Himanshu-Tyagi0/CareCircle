"""Pure adherence metrics, on-time/late breakdown, streak calculation, and heatmap tables."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from core.models import ActorContext, DoseState
from core.models_clinical import DoseInstance
from core.permissions import Action, authorize
from core.security import neutralize_csv_cell


@dataclass(frozen=True)
class MedicationAdherence:
    """Per-medication confirmation breakdown with explicit denominator."""

    medication_id: UUID
    medication_name: str
    total_past_due: int
    taken: int
    on_time: int
    late: int
    cant_take: int
    skipped: int
    unconfirmed: int
    rate_percent: float | None


@dataclass(frozen=True)
class AdherenceSummary:
    """Overall window adherence summary and accessible weekly heatmap rows."""

    window_days: int
    start_date: date
    end_date: date
    total_past_due: int
    taken: int
    on_time: int
    late: int
    cant_take: int
    skipped: int
    unconfirmed: int
    rate_percent: float | None
    streak_days: int
    by_medication: tuple[MedicationAdherence, ...]
    daily_rows: tuple[dict[str, object], ...]


def compute_streak(
    doses: list[DoseInstance],
    *,
    tz_name: str,
    today_local: date,
) -> int:
    """Compute consecutive completed local days where all scheduled doses were taken.

    No-dose days neither earn nor break a streak. Today's unfinished schedule does
    not prematurely break yesterday's streak.
    """
    tz = ZoneInfo(tz_name)
    by_day: dict[date, list[DoseInstance]] = defaultdict(list)
    for d in doses:
        if d.state != DoseState.CANCELLED:
            by_day[d.due_at.astimezone(tz).date()].append(d)

    streak = 0
    if today_local in by_day:
        today_doses = by_day[today_local]
        if today_doses and all(d.state == DoseState.TAKEN for d in today_doses):
            streak += 1

    # Walk backwards through prior days that had scheduled doses
    min_day = min(by_day.keys(), default=today_local)
    cursor = today_local - timedelta(days=1)
    while cursor >= min_day:
        day_doses = by_day.get(cursor)
        if not day_doses:
            cursor -= timedelta(days=1)
            continue
        if all(d.state == DoseState.TAKEN for d in day_doses):
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


def compute_adherence(
    doses: list[DoseInstance],
    *,
    window_days: int,
    tz_name: str,
    grace_minutes: int,
    now: datetime,
) -> AdherenceSummary:
    """Pure transparent adherence calculation excluding cancelled and future doses."""
    tz = ZoneInfo(tz_name)
    today_local = now.astimezone(tz).date()
    start_local = today_local - timedelta(days=window_days - 1)

    past_due = [
        d
        for d in doses
        if d.state != DoseState.CANCELLED
        and d.due_at <= now
        and start_local <= d.due_at.astimezone(tz).date() <= today_local
    ]

    med_groups: dict[UUID, list[DoseInstance]] = defaultdict(list)
    for d in past_due:
        med_groups[d.medication_id].append(d)

    by_med: list[MedicationAdherence] = []
    for med_id, items in sorted(med_groups.items(), key=lambda kv: kv[1][0].medication_name):
        total = len(items)
        taken_items = [x for x in items if x.state == DoseState.TAKEN]
        on_time = sum(
            1
            for x in taken_items
            if x.confirmed_at is not None
            and (x.confirmed_at - x.due_at) <= timedelta(minutes=grace_minutes)
        )
        late = len(taken_items) - on_time
        cant_take = sum(1 for x in items if x.state == DoseState.CANT_TAKE)
        skipped = sum(1 for x in items if x.state == DoseState.SKIPPED)
        unconfirmed = total - len(taken_items) - cant_take - skipped
        rate = round((len(taken_items) / total) * 100.0, 1) if total > 0 else None
        by_med.append(
            MedicationAdherence(
                medication_id=med_id,
                medication_name=items[0].medication_name,
                total_past_due=total,
                taken=len(taken_items),
                on_time=on_time,
                late=late,
                cant_take=cant_take,
                skipped=skipped,
                unconfirmed=unconfirmed,
                rate_percent=rate,
            )
        )

    total_all = len(past_due)
    taken_all = sum(m.taken for m in by_med)
    on_time_all = sum(m.on_time for m in by_med)
    late_all = sum(m.late for m in by_med)
    cant_take_all = sum(m.cant_take for m in by_med)
    skipped_all = sum(m.skipped for m in by_med)
    unconfirmed_all = sum(m.unconfirmed for m in by_med)
    overall_rate = round((taken_all / total_all) * 100.0, 1) if total_all > 0 else None

    daily_rows: list[dict[str, object]] = []
    week_start = today_local - timedelta(days=6)
    for offset in range(7):
        day_d = week_start + timedelta(days=offset)
        day_items = [d for d in past_due if d.due_at.astimezone(tz).date() == day_d]
        d_total = len(day_items)
        d_taken = sum(1 for d in day_items if d.state == DoseState.TAKEN)
        daily_rows.append(
            {
                "date": day_d.isoformat(),
                "total": d_total,
                "taken": d_taken,
                "cant_take": sum(1 for d in day_items if d.state == DoseState.CANT_TAKE),
                "skipped": sum(1 for d in day_items if d.state == DoseState.SKIPPED),
                "unconfirmed": sum(
                    1
                    for d in day_items
                    if d.state in (DoseState.PENDING, DoseState.SNOOZED, DoseState.EXPIRED)
                ),
                "rate_percent": round((d_taken / d_total) * 100.0, 1) if d_total > 0 else None,
            }
        )

    streak = compute_streak(doses, tz_name=tz_name, today_local=today_local)
    return AdherenceSummary(
        window_days=window_days,
        start_date=start_local,
        end_date=today_local,
        total_past_due=total_all,
        taken=taken_all,
        on_time=on_time_all,
        late=late_all,
        cant_take=cant_take_all,
        skipped=skipped_all,
        unconfirmed=unconfirmed_all,
        rate_percent=overall_rate,
        streak_days=streak,
        by_medication=tuple(by_med),
        daily_rows=tuple(daily_rows),
    )


def get_authorized_adherence(
    actor: ActorContext,
    *,
    doses: list[DoseInstance],
    window_days: int,
    grace_minutes: int,
    now: datetime,
) -> AdherenceSummary:
    """Enforce ADHERENCE consent before returning adherence analytics."""
    authorize(actor, Action.VIEW_ADHERENCE, target_circle_id=actor.circle_id)
    return compute_adherence(
        doses,
        window_days=window_days,
        tz_name=actor.circle_timezone,
        grace_minutes=grace_minutes,
        now=now,
    )


def export_adherence_csv(doses: list[DoseInstance]) -> str:
    """Export dose history to CSV with spreadsheet formula-injection neutralization."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "due_at", "medication", "dose", "state", "confirmed_at", "note"])
    for d in sorted(doses, key=lambda x: x.due_at):
        writer.writerow(
            [
                neutralize_csv_cell(d.local_date.isoformat()),
                neutralize_csv_cell(d.due_at.isoformat()),
                neutralize_csv_cell(d.medication_name),
                neutralize_csv_cell(d.dose_text),
                neutralize_csv_cell(d.state.value),
                neutralize_csv_cell(d.confirmed_at.isoformat() if d.confirmed_at else ""),
                neutralize_csv_cell(d.cant_take_note or d.skip_reason or ""),
            ]
        )
    return buf.getvalue()
