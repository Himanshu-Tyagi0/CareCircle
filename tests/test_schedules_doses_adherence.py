"""Tests for DST/timezone schedules, dose lifecycle, inventory, adherence, streaks, and CSV export."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from core.errors import ValidationError
from core.models import CantTakeReason, Criticality, DoseState, Role
from services.adherence_service import compute_adherence
from services.container import ServiceContainer
from services.medication_service import compute_refill_forecast
from services.schedule_service import expand_schedule
from tests.conftest import FrozenClock, invite_and_join, onboard_elder


@pytest.mark.unit
def test_dst_spring_forward_and_fall_back_schedule_expansion() -> None:
    """Spring-forward nonexistent time shifts forward with label; fall-back creates fold=0 single dose."""
    import uuid

    from core.models_clinical import Medication, MedicationSchedule

    ny = ZoneInfo("America/New_York")
    # 2026-03-08 is US Spring Forward (02:00 -> 03:00)
    med = Medication(
        id=uuid.uuid4(),
        circle_id=uuid.uuid4(),
        name="Thyroid",
        dose_text="50 mcg",
        start_date=date(2026, 3, 1),
        criticality=Criticality.NORMAL,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        updated_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    sched = MedicationSchedule(
        id=uuid.uuid4(),
        circle_id=med.circle_id,
        medication_id=med.id,
        version=1,
        times_local=(time(2, 30),),
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        start_date=date(2026, 3, 1),
        timezone="America/New_York",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    spring_start = datetime(2026, 3, 8, 0, 0, tzinfo=ny).astimezone(UTC)
    spring_end = datetime(2026, 3, 9, 0, 0, tzinfo=ny).astimezone(UTC)
    spring_doses = expand_schedule(
        medication=med,
        schedule=sched,
        window_start_utc=spring_start,
        window_end_utc=spring_end,
        now=spring_start,
    )
    assert len(spring_doses) == 1
    assert "shifted_from_02:30" in spring_doses[0].dst_adjustment_note
    assert spring_doses[0].due_at.astimezone(ny).hour == 3

    # 2026-11-01 is US Fall Back (01:00-02:00 occurs twice)
    sched_fb = sched.model_copy(update={"times_local": (time(1, 30),)})
    fall_start = datetime(2026, 11, 1, 0, 0, tzinfo=ny).astimezone(UTC)
    fall_end = datetime(2026, 11, 2, 0, 0, tzinfo=ny).astimezone(UTC)
    fall_doses = expand_schedule(
        medication=med,
        schedule=sched_fb,
        window_start_utc=fall_start,
        window_end_utc=fall_end,
        now=fall_start,
    )
    assert len(fall_doses) == 1
    # Must be fold=0 (EDT, UTC-4 -> 05:30 UTC)
    assert fall_doses[0].due_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


@pytest.mark.unit
def test_refill_forecast_and_occurrence_expiry() -> None:
    """Refill forecast returns unavailable when unset, exact date when set; expiry uses min(next, +12h)."""
    import uuid

    from core.models_clinical import Medication, MedicationSchedule

    med = Medication(
        id=uuid.uuid4(),
        circle_id=uuid.uuid4(),
        name="MedA",
        dose_text="1 tab",
        start_date=date(2026, 5, 1),
        stock_count=10.0,
        stock_units_per_dose=1.0,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    sched = MedicationSchedule(
        id=uuid.uuid4(),
        circle_id=med.circle_id,
        medication_id=med.id,
        version=1,
        times_local=(time(8, 0), time(12, 0)),  # 4h gap between 08:00 and 12:00
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        start_date=date(2026, 5, 1),
        timezone="UTC",
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    fc = compute_refill_forecast(medication=med, schedule=sched, today_local=date(2026, 5, 10))
    assert fc.available is True
    assert fc.days_remaining == 5
    assert fc.estimated_runout_date == date(2026, 5, 15)

    fc_untracked = compute_refill_forecast(
        medication=med.model_copy(update={"stock_count": None}),
        schedule=sched,
        today_local=date(2026, 5, 10),
    )
    assert fc_untracked.available is False

    # Check occurrence expiry: 08:00 expires at 12:00 (4h), 12:00 expires at 24:00 (12h)
    doses = expand_schedule(
        medication=med,
        schedule=sched,
        window_start_utc=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        window_end_utc=datetime(2026, 5, 11, 0, 0, tzinfo=UTC),
        now=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
    )
    assert len(doses) == 2
    assert doses[0].expires_at - doses[0].due_at == timedelta(hours=4)
    assert doses[1].expires_at - doses[1].due_at == timedelta(hours=12)


@pytest.mark.integration
def test_dose_lifecycle_double_tap_zero_stock_cant_take_skip_and_correction(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Double-tap taken decrements once; zero stock sets discrepancy; cant_take/skip/correct work."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, _ = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )

    med, _ = container.medication.create_medication(
        elder,
        name="Amlodipine",
        dose_text="5 mg",
        instructions="Morning",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 12),
        criticality=Criticality.CRITICAL,
        stock_count=1.0,
        stock_unit="tablets",
        refill_threshold=2.0,
        stock_units_per_dose=1.0,
        times_local=[time(8, 0), time(20, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    # create_medication already materialized initial 48h horizon; extending clock adds future doses idempotently
    ins0 = container.schedule.generate_horizon_for_circle(elder.circle_id, horizon_hours=48)
    assert ins0 == 0
    doses = container.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=48)
    )
    assert len(doses) >= 4
    clock_saved = clock.now()
    clock.advance(hours=24)
    ins1 = container.schedule.generate_horizon_for_circle(elder.circle_id, horizon_hours=48)
    ins2 = container.schedule.generate_horizon_for_circle(elder.circle_id, horizon_hours=48)
    assert ins1 >= 1
    assert ins2 == 0
    clock.set(clock_saved)

    doses = container.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=48)
    )
    d0, d1, d2 = doses[0], doses[1], doses[2]

    # Snooze preserves original due_at
    snz = container.dose.snooze_dose(elder, dose_id=d0.id, minutes=10)
    assert snz.dose.due_at == d0.due_at
    assert snz.dose.state == DoseState.SNOOZED

    # Mark taken twice -> single stock decrement (1 -> 0) and low stock alert queued
    res1 = container.dose.mark_taken(elder, dose_id=d0.id)
    assert res1.already_confirmed is False
    assert res1.inventory_discrepancy is False
    res2 = container.dose.mark_taken(elder, dose_id=d0.id)
    assert res2.already_confirmed is True
    med_after = container.repos.medication.get_medication(elder.circle_id, med.id)
    assert med_after is not None and med_after.stock_count == 0.0

    # Zero-stock dose taken -> flags inventory_discrepancy and never negative
    res3 = container.dose.mark_taken(elder, dose_id=d1.id)
    assert res3.inventory_discrepancy is True
    med_after2 = container.repos.medication.get_medication(elder.circle_id, med.id)
    assert med_after2 is not None and med_after2.stock_count == 0.0

    # Can't take with Other requires non-empty note
    with pytest.raises(ValidationError):
        container.dose.report_cant_take(
            elder, dose_id=d2.id, reason=CantTakeReason.OTHER, note="  "
        )
    ct_res = container.dose.report_cant_take(
        elder, dose_id=d2.id, reason=CantTakeReason.NAUSEA_OR_SIDE_EFFECT, note="Dizzy"
    )
    assert ct_res.dose.state == DoseState.CANT_TAKE

    # Explicit skip requires reason
    with pytest.raises(ValidationError):
        container.dose.skip_dose(elder, dose_id=doses[3].id, reason="")
    sk_res = container.dose.skip_dose(elder, dose_id=doses[3].id, reason="Fasting today")
    assert sk_res.dose.state == DoseState.SKIPPED

    # Audited correction from TAKEN back to SKIPPED restores stock by 1
    with pytest.raises(ValidationError):
        container.dose.correct_dose(care, dose_id=d0.id, new_state=DoseState.SKIPPED, reason="")
    corr = container.dose.correct_dose(
        care, dose_id=d0.id, new_state=DoseState.SKIPPED, reason="Tapped wrong pill"
    )
    assert corr.dose.state == DoseState.SKIPPED
    med_after_corr = container.repos.medication.get_medication(elder.circle_id, med.id)
    assert med_after_corr is not None and med_after_corr.stock_count == 1.0


@pytest.mark.unit
def test_adherence_zero_denominator_late_streak_and_csv_export() -> None:
    """Zero past-due returns None; no-dose day neutral; today pending preserves yesterday streak."""
    import uuid

    from core.models_clinical import DoseInstance
    from services.adherence_service import export_adherence_csv

    now = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)
    assert (
        compute_adherence([], window_days=7, tz_name="UTC", grace_minutes=30, now=now).rate_percent
        is None
    )

    med_id = uuid.uuid4()
    cid = uuid.uuid4()
    sid = uuid.uuid4()
    # Day -2 (May 8): taken on time; Day -1 (May 9): no doses (neutral!); Today (May 10 18:00): future pending
    d_may8 = DoseInstance(
        id=uuid.uuid4(),
        circle_id=cid,
        medication_id=med_id,
        schedule_id=sid,
        schedule_version=1,
        medication_name="=EVIL_MED",
        dose_text="1 tab",
        criticality=Criticality.NORMAL,
        due_at=datetime(2026, 5, 8, 8, 0, tzinfo=UTC),
        local_date=date(2026, 5, 8),
        local_time_intended=time(8, 0),
        expires_at=datetime(2026, 5, 8, 20, 0, tzinfo=UTC),
        state=DoseState.TAKEN,
        confirmed_at=datetime(2026, 5, 8, 8, 10, tzinfo=UTC),
        created_at=now,
        updated_at=now,
    )
    d_may10_late = DoseInstance(
        id=uuid.uuid4(),
        circle_id=cid,
        medication_id=med_id,
        schedule_id=sid,
        schedule_version=1,
        medication_name="=EVIL_MED",
        dose_text="1 tab",
        criticality=Criticality.NORMAL,
        due_at=datetime(2026, 5, 10, 8, 0, tzinfo=UTC),
        local_date=date(2026, 5, 10),
        local_time_intended=time(8, 0),
        expires_at=datetime(2026, 5, 10, 20, 0, tzinfo=UTC),
        state=DoseState.PENDING,
        created_at=now,
        updated_at=now,
    )
    summary = compute_adherence(
        [d_may8, d_may10_late], window_days=7, tz_name="UTC", grace_minutes=30, now=now
    )
    assert summary.total_past_due == 2
    assert summary.taken == 1
    assert summary.rate_percent == 50.0
    # Today's unfinished schedule does not prematurely break yesterday's streak; missing a prior day does
    assert summary.streak_days == 1

    d_may9_missed = d_may10_late.model_copy(
        update={
            "id": uuid.uuid4(),
            "due_at": datetime(2026, 5, 9, 8, 0, tzinfo=UTC),
            "local_date": date(2026, 5, 9),
            "expires_at": datetime(2026, 5, 9, 20, 0, tzinfo=UTC),
            "state": DoseState.EXPIRED,
        }
    )
    summary2 = compute_adherence(
        [d_may8, d_may9_missed, d_may10_late],
        window_days=7,
        tz_name="UTC",
        grace_minutes=30,
        now=now,
    )
    assert summary2.streak_days == 0

    # CSV export neutralizes formula cells
    csv_text = export_adherence_csv([d_may8])
    assert "'=EVIL_MED" in csv_text
