"""Tests for pure Care Chain escalation, quiet hours, delayed cron, check-ins, and urgent help."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from core.models import CantTakeReason, CheckInMood, Criticality, DoseState, OutboxStatus, Role
from services.checkin_service import CheckInPolicy, compute_checkin_streak, next_checkin_action
from services.container import ServiceContainer
from services.escalation_service import (
    DoseStatusSnapshot,
    EscalationProgress,
    LevelDisposition,
    Policy,
    is_in_quiet_hours,
    next_action,
)
from tests.conftest import FakeEmailGateway, FrozenClock, invite_and_join, onboard_elder


@pytest.mark.unit
def test_care_chain_criticalities_snooze_ack_and_delayed_cron() -> None:
    """Pure next_action handles criticalities, snooze, ack, expiry, and delayed cron superseding."""
    due = datetime(2026, 5, 10, 8, 0, tzinfo=UTC)
    p_norm = Policy(criticality=Criticality.NORMAL, timezone="UTC")
    p_crit = Policy(criticality=Criticality.CRITICAL, timezone="UTC")
    p_opt = Policy(criticality=Criticality.OPTIONAL, timezone="UTC")

    # Terminal states return none
    for term in (DoseState.TAKEN, DoseState.SKIPPED, DoseState.CANCELLED, DoseState.EXPIRED):
        assert (
            next_action(
                due, due, DoseStatusSnapshot(state=term), EscalationProgress(), p_norm, False
            ).kind
            == "none"
        )

    # Snoozed suppresses until snoozed_until
    snz = DoseStatusSnapshot(state=DoseState.SNOOZED, snoozed_until=due + timedelta(minutes=10))
    assert (
        next_action(due, due + timedelta(minutes=5), snz, EscalationProgress(), p_norm, False).kind
        == "none"
    )

    # Acknowledged stops chain
    assert (
        next_action(
            due,
            due + timedelta(minutes=65),
            DoseStatusSnapshot(state=DoseState.PENDING),
            EscalationProgress(),
            p_norm,
            True,
        ).kind
        == "none"
    )

    # Expired returns none
    assert (
        next_action(
            due,
            due + timedelta(hours=13),
            DoseStatusSnapshot(state=DoseState.PENDING),
            EscalationProgress(),
            p_norm,
            False,
        ).kind
        == "none"
    )

    # Optional never escalates beyond level 1
    a1 = next_action(
        due,
        due + timedelta(minutes=120),
        DoseStatusSnapshot(state=DoseState.PENDING),
        EscalationProgress(),
        p_opt,
        False,
    )
    assert a1.level == 1
    assert a1.next_progress is not None
    assert (
        next_action(
            due,
            due + timedelta(minutes=150),
            DoseStatusSnapshot(state=DoseState.PENDING),
            a1.next_progress,
            p_opt,
            False,
        ).kind
        == "none"
    )

    # Critical escalates to level 4 at 40m; delayed cron emits ONLY level 4 and supersedes 1..3
    delayed = next_action(
        due,
        due + timedelta(minutes=45),
        DoseStatusSnapshot(state=DoseState.PENDING),
        EscalationProgress(),
        p_crit,
        False,
    )
    assert delayed.level == 4
    assert delayed.kind == "secondary_contact"
    assert delayed.next_progress is not None
    assert delayed.next_progress.dispositions[1] == LevelDisposition.SUPERSEDED
    assert delayed.next_progress.dispositions[2] == LevelDisposition.SUPERSEDED
    assert delayed.next_progress.dispositions[3] == LevelDisposition.SUPERSEDED
    assert delayed.next_progress.dispositions[4] == LevelDisposition.QUEUED


@pytest.mark.unit
def test_care_chain_quiet_hours_and_cant_take_override() -> None:
    """Quiet hours suppress non-critical contacts without replay; can't_take sends once even in quiet/acked."""
    due = datetime(2026, 5, 10, 22, 30, tzinfo=UTC)
    policy = Policy(
        criticality=Criticality.NORMAL,
        timezone="UTC",
        quiet_hours_enabled=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        elder_quiet_reminders_enabled=False,
    )
    assert is_in_quiet_hours(datetime(2026, 5, 10, 23, 0, tzinfo=UTC), policy) is True
    assert is_in_quiet_hours(datetime(2026, 5, 11, 7, 1, tzinfo=UTC), policy) is False

    # During quiet hours (23:30 UTC), normal dose is suppressed
    quiet_act = next_action(
        due,
        datetime(2026, 5, 10, 23, 30, tzinfo=UTC),
        DoseStatusSnapshot(state=DoseState.PENDING),
        EscalationProgress(),
        policy,
        False,
    )
    assert quiet_act.kind == "none"

    # When quiet hours end at 07:01 UTC, emits only highest eligible level (4), no burst for 1..3
    after_quiet = next_action(
        due,
        datetime(2026, 5, 11, 7, 1, tzinfo=UTC),
        DoseStatusSnapshot(state=DoseState.PENDING),
        EscalationProgress(),
        policy,
        False,
    )
    assert after_quiet.level == 4
    assert after_quiet.next_progress is not None
    assert after_quiet.next_progress.dispositions[1] == LevelDisposition.SUPERSEDED

    # Can't take sends explicit help EVEN during quiet hours and EVEN after earlier ack,
    # and editing reason with same report_id does not re-send!
    ct_snap = DoseStatusSnapshot(
        state=DoseState.CANT_TAKE,
        cant_take_report_id="rep-1",
        cant_take_reason=CantTakeReason.OUT_OF_STOCK,
    )
    ct_act = next_action(
        due,
        datetime(2026, 5, 10, 23, 30, tzinfo=UTC),
        ct_snap,
        EscalationProgress(high_water_level=2),
        policy,
        True,
    )
    assert ct_act.kind == "explicit_help_report"
    assert ct_act.next_progress is not None
    assert (
        next_action(
            due,
            datetime(2026, 5, 10, 23, 35, tzinfo=UTC),
            ct_snap,
            ct_act.next_progress,
            policy,
            True,
        ).kind
        == "none"
    )


@pytest.mark.unit
def test_pure_checkin_escalation_and_streak() -> None:
    """Pure check-in escalation and streak preserve yesterday across unfinished today."""
    import uuid

    from core.models_clinical import CheckInInstance

    due = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
    win_end = due + timedelta(hours=2)
    exp = due + timedelta(hours=24)
    pol = CheckInPolicy(timezone="UTC")

    a1 = next_checkin_action(
        due_at=due,
        window_ends_at=win_end,
        expires_at=exp,
        now=due,
        responded=False,
        mood=None,
        sent_levels=EscalationProgress(),
        policy=pol,
        acknowledged=False,
    )
    assert a1.kind == "checkin_elder_reminder"

    # Past window emits level 2 contact reminder
    a2 = next_checkin_action(
        due_at=due,
        window_ends_at=win_end,
        expires_at=exp,
        now=win_end + timedelta(minutes=5),
        responded=False,
        mood=None,
        sent_levels=a1.next_progress or EscalationProgress(),
        policy=pol,
        acknowledged=False,
    )
    assert a2.kind == "checkin_unanswered_contact"

    # Streak calculation
    cid = uuid.uuid4()
    c_yest = CheckInInstance(
        id=uuid.uuid4(),
        circle_id=cid,
        local_date=date(2026, 5, 9),
        due_at=due - timedelta(days=1),
        window_ends_at=win_end - timedelta(days=1),
        expires_at=due,
        mood=CheckInMood.GOOD,
        responded_at=due - timedelta(days=1),
        created_at=due,
        updated_at=due,
    )
    c_today = CheckInInstance(
        id=uuid.uuid4(),
        circle_id=cid,
        local_date=date(2026, 5, 10),
        due_at=due,
        window_ends_at=win_end,
        expires_at=exp,
        created_at=due,
        updated_at=due,
    )
    assert (
        compute_checkin_streak(
            [c_yest, c_today],
            enabled_from_local=date(2026, 5, 9),
            today_local=date(2026, 5, 10),
            current_day_window_closed=False,
        )
        == 1
    )
    assert (
        compute_checkin_streak(
            [c_yest, c_today],
            enabled_from_local=date(2026, 5, 9),
            today_local=date(2026, 5, 10),
            current_day_window_closed=True,
        )
        == 0
    )


@pytest.mark.integration
def test_checkin_not_well_toggle_and_urgent_help(
    container: ServiceContainer, clock: FrozenClock, email_gw: FakeEmailGateway
) -> None:
    """Not well toggle back cancels unsent, reactivates same logical key, and urgent help is distinct."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    container.circle.update_policies(elder, primary_contact_membership_id=care_mem_id)
    email_gw.sent.clear()

    # First check-in Not well -> sent immediately
    ci1, status1 = container.checkin.submit_checkin(elder, mood=CheckInMood.NOT_WELL)
    assert status1 == "provider_accepted"
    assert len(email_gw.sent) == 1

    # Toggling back to Not well again doesn't duplicate because already SENT
    _, status2 = container.checkin.submit_checkin(elder, mood=CheckInMood.NOT_WELL)
    assert status2 == "already_processed"
    assert len(email_gw.sent) == 1

    # Simulate unsent Not well (failure queued as retryable), toggle to Okay -> cancels, toggle back -> reactivates same key
    from integrations.email_gateway import DeliveryState

    clock.advance(days=1)
    email_gw.next_state = DeliveryState.DEFINITIVE_FAILURE
    ci2, status_fail = container.checkin.submit_checkin(elder, mood=CheckInMood.NOT_WELL)
    assert status_fail == "failed_retryable"
    lkey = f"checkin_not_well:{ci2.id}:{care_mem_id}:email"
    out_before = container.repos.outbox.get_outbox_by_logical_key(lkey)
    assert out_before is not None and out_before.status == OutboxStatus.RETRYABLE

    # Toggle away -> cancelled
    container.checkin.submit_checkin(elder, mood=CheckInMood.OKAY)
    out_cancelled = container.repos.outbox.get_outbox_by_logical_key(lkey)
    assert out_cancelled is not None and out_cancelled.status == OutboxStatus.CANCELLED

    # Toggle back -> reactivates SAME logical key and sends when gateway recovers
    email_gw.next_state = DeliveryState.ACCEPTED
    _, status_rec = container.checkin.submit_checkin(elder, mood=CheckInMood.NOT_WELL)
    assert status_rec == "provider_accepted"
    out_sent = container.repos.outbox.get_outbox_by_logical_key(lkey)
    assert out_sent is not None and out_sent.status == OutboxStatus.SENT

    # Urgent help uses a separate event type
    before_count = len(email_gw.sent)
    uh_status = container.checkin.trigger_urgent_help(elder)
    assert uh_status == "provider_accepted"
    assert len(email_gw.sent) == before_count + 1
    assert "URGENT" in email_gw.sent[-1].subject
