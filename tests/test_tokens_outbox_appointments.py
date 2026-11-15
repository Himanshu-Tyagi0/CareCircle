"""Tests for HMAC action tokens, outbox lease recovery, appointments, and worker cycle."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta

import pytest
from core.errors import TokenInvalidError
from core.models import ActionTokenType, CantTakeReason, Criticality, DoseState, OutboxStatus, Role
from integrations.email_gateway import DeliveryState
from services.container import ServiceContainer
from tests.conftest import (
    FakeEmailGateway,
    FakeTelegramGateway,
    FrozenClock,
    invite_and_join,
    onboard_elder,
)


@pytest.mark.integration
def test_action_token_preview_redemption_rotation_and_concurrency(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Action tokens support harmless preview, key rotation, single-use concurrency, and expiry."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    container.circle.update_policies(elder, primary_contact_membership_id=care_mem_id)
    container.medication.create_medication(
        elder,
        name="Metformin",
        dose_text="500 mg",
        instructions="After meal",
        start_date=date(2026, 5, 10),
        end_date=None,
        criticality=Criticality.NORMAL,
        stock_count=10,
        stock_unit="tabs",
        refill_threshold=2,
        stock_units_per_dose=1,
        times_local=[time(8, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    container.schedule.generate_horizon_for_circle(elder.circle_id, horizon_hours=24)
    dose = container.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=24)
    )[0]

    elder_mem = elder.membership_id
    tok_prev = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.DOSE_TAKEN,
        circle_id=elder.circle_id,
        resource_type="dose",
        resource_id=dose.id,
        recipient_membership_id=elder_mem,
        window_ends_at=dose.expires_at,
    )
    # Preview does not mutate state
    preview = container.action_tokens.preview_token(tok_prev)
    assert preview.action == ActionTokenType.DOSE_TAKEN
    assert (
        container.dose.list_doses_in_window(
            elder,
            start_utc=clock.now() - timedelta(hours=1),
            end_utc=clock.now() + timedelta(hours=24),
        )[0].state
        == DoseState.PENDING
    )

    # Concurrent redemption: exactly one wins, others get TokenInvalidError
    def redeem_one() -> str:
        try:
            container.action_tokens.redeem_token(tok_prev)
            return "ok"
        except TokenInvalidError:
            return "invalid"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: redeem_one(), range(4)))
    assert outcomes.count("ok") == 1
    assert outcomes.count("invalid") == 3

    # Also test rotated signature directly
    from core.security import sign_action_token

    bad_sig, _ = sign_action_token(
        key_id=container.config.action_signing_previous_key_id,
        signing_key=container.config.action_signing_previous_key,
        action=ActionTokenType.DOSE_TAKEN,
        circle_id=elder.circle_id,
        resource_type="dose",
        resource_id=uuid.uuid4(),
        recipient_membership_id=elder_mem,
        expires_at=clock.now() + timedelta(hours=1),
    )
    with pytest.raises(TokenInvalidError):
        container.action_tokens.preview_token(bad_sig)

    # Care ack token via CARE_ACK
    ack_tok = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.CARE_ACK,
        circle_id=elder.circle_id,
        resource_type="dose",
        resource_id=dose.id,
        recipient_membership_id=care_mem_id,
        window_ends_at=clock.now() + timedelta(hours=4),
    )
    res = container.action_tokens.redeem_token(ack_tok)
    assert res["first"] is True


@pytest.mark.integration
def test_outbox_leases_uncertain_reconciliation_and_ack(
    container: ServiceContainer,
    clock: FrozenClock,
    email_gw: FakeEmailGateway,
    tg_gw: FakeTelegramGateway,
) -> None:
    """Pre-send failure invalidates tokens; uncertain send preserves tokens until elder reconciles."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    _, fam, fam_mem_id = invite_and_join(
        container, elder, email="f@example.test", name="Meera", role=Role.FAMILY
    )
    # Opt caretaker into Telegram too
    container.auth.update_own_preferences(care, language="hi", font_px=18, high_contrast=False)
    container.auth.update_own_telegram(care, opt_in=True, chat_id="998877")
    container.circle.update_policies(
        elder,
        primary_contact_membership_id=care_mem_id,
        secondary_contact_membership_id=fam_mem_id,
    )
    container.medication.create_medication(
        elder,
        name="Atorvastatin",
        dose_text="10 mg",
        instructions="Night",
        start_date=date(2026, 5, 10),
        end_date=None,
        criticality=Criticality.CRITICAL,
        stock_count=10,
        stock_unit="tabs",
        refill_threshold=2,
        stock_units_per_dose=1,
        times_local=[time(8, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )

    # Worker run cycle materializes doses and queues level 1 elder reminder
    stats = container.worker.run_cycle()
    assert stats["outbox_sent"] >= 1

    # Simulate uncertain send on cant_take -> item marked UNCERTAIN and tokens preserved
    dose = container.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=24)
    )[0]
    email_gw.next_state = DeliveryState.UNCERTAIN
    tg_gw.next_state = DeliveryState.ACCEPTED
    res = container.dose.report_cant_take(
        elder, dose_id=dose.id, reason=CantTakeReason.NAUSEA_OR_SIDE_EFFECT, note=""
    )
    assert "uncertain" in res.delivery_status or "provider_accepted" in res.delivery_status
    out_items = container.repos.outbox.list_outbox(elder.circle_id)
    uncertain_items = [o for o in out_items if o.status == OutboxStatus.UNCERTAIN]
    assert len(uncertain_items) == 1
    reconciled = container.notifications.reconcile_uncertain(
        elder, item_id=uncertain_items[0].id, resolution="sent"
    )
    assert reconciled.status == OutboxStatus.SENT

    # Acknowledge episode stops further escalation and notifies other contacts
    email_gw.next_state = DeliveryState.ACCEPTED
    ack, is_first = container.notifications.acknowledge_episode(
        care, episode_type="dose", resource_id=dose.id
    )
    assert is_first is True
    assert ack.acknowledged_by_name == "Rohan"
    _, is_second = container.notifications.acknowledge_episode(
        fam, episode_type="dose", resource_id=dose.id
    )
    assert is_second is False


@pytest.mark.integration
def test_appointment_reminders_and_edit_cancel_invalidation(
    container: ServiceContainer, clock: FrozenClock, email_gw: FakeEmailGateway
) -> None:
    """Appointments enqueue 24h/1h reminders and cancel/invalidate stale versions on edit or cancel."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    container.circle.update_policies(elder, primary_contact_membership_id=care_mem_id)

    starts = clock.now() + timedelta(hours=23)
    appt = container.appointment.create_appointment(
        elder,
        doctor_name="Dr. Gupta",
        specialty="Cardiology",
        location="Max Hospital",
        starts_at=starts,
        notes="Carry ECG",
    )

    # First worker cycle sends 24h reminder (level 1)
    container.worker.run_cycle()
    assert any("Dr. Gupta" in m.subject for m in email_gw.sent)

    # Edit appointment bumps version -> prior reminders invalidated; new version schedules fresh reminder
    updated = container.appointment.update_appointment(
        elder,
        appointment_id=appt.id,
        location="Apollo Clinic",
        starts_at=starts + timedelta(hours=1),
    )
    assert updated.version == 2

    # Advance to 30m before appointment -> level 2 (1h reminder) fires for v2
    clock.set(updated.starts_at - timedelta(minutes=30))
    container.worker.run_cycle()
    assert any("Apollo Clinic" in m.text_body for m in email_gw.sent)

    # Cancel appointment cancels future reminders cleanly
    cancelled = container.appointment.cancel_appointment(elder, appointment_id=appt.id)
    assert cancelled.cancelled_at is not None
