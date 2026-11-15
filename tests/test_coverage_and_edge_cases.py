"""Targeted edge-case and branch coverage tests for core/ and services/."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from core.clock import FixedClock, SystemClock
from core.config import AppConfig, load_config
from core.errors import (
    AuthorizationError,
    ConfigurationError,
    ConsentRequiredError,
    NotFoundError,
    TokenInvalidError,
    ValidationError,
)
from core.i18n import (
    format_local_date,
    format_local_datetime,
    format_local_time,
    t,
    validate_locale_parity,
)
from core.models import (
    ActionTokenType,
    ActorContext,
    CantTakeReason,
    CheckInMood,
    ConsentCategory,
    Criticality,
    DoseState,
    FamilyGrant,
    NotificationChannel,
    OutboxStatus,
    Role,
)
from core.models_clinical import (
    CheckInInstance,
    Medication,
    MedicationSchedule,
    NotificationOutboxItem,
)
from core.permissions import Action, authorize
from core.security import (
    MAX_PASSCODE_UTF8_BYTES,
    hash_passcode,
    sign_action_token,
    verify_action_token,
)
from services.appointment_service import select_appointment_reminder_level
from services.checkin_service import CheckInPolicy, compute_checkin_streak, next_checkin_action
from services.container import ServiceContainer, build_container
from services.escalation_service import EscalationProgress, LevelDisposition
from services.medication_service import compute_refill_forecast
from tests.conftest import (
    FakeEmailGateway,
    FakeTelegramGateway,
    FrozenClock,
    invite_and_join,
    make_test_config,
    onboard_elder,
)


@pytest.mark.unit
def test_clock_config_i18n_permissions_and_security_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover clock helpers, env config parsing, i18n fallbacks, RBAC matrix, and token errors."""
    sys_clk = SystemClock()
    assert sys_clk.now().tzinfo is not None

    naive_dt = datetime(2026, 5, 10, 12, 0)
    fixed_dt = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    fclk = FixedClock(naive_dt)
    assert fclk.now() == fixed_dt
    fclk.set(naive_dt + timedelta(hours=2))
    assert fclk.now() == fixed_dt + timedelta(hours=2)
    fclk.advance(minutes=30)
    assert fclk.now() == fixed_dt + timedelta(hours=2, minutes=30)

    monkeypatch.setenv("CARECIRCLE_DEMO_MODE", "true")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "care@example.test")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", "care@example.test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg_tok")
    monkeypatch.setenv("GEMINI_API_KEY", "gem_key")
    cfg = load_config()
    assert cfg.demo_mode is True
    assert cfg.has_smtp is True
    assert cfg.has_telegram is True
    assert cfg.has_gemini is True

    monkeypatch.setenv("BCRYPT_ROUNDS", "not-an-int")
    cfg2 = load_config()
    assert cfg2.bcrypt_rounds == 12

    bad_prod = AppConfig(
        demo_mode=False,
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="srk",
        action_signing_key="short",
    )
    with pytest.raises(ConfigurationError):
        bad_prod.require_production_ready()

    # i18n fallbacks, locale parity & formatting
    from core.permissions import build_permission_matrix

    assert validate_locale_parity() == []
    assert t("missing.nonexistent.key", lang="fr") == "missing.nonexistent.key"
    assert format_local_date(date(2026, 5, 10), "Asia/Kolkata", lang="hi")
    assert format_local_time(fixed_dt, "Asia/Kolkata", lang="en")
    assert format_local_datetime(fixed_dt, "Asia/Kolkata", lang="hi")

    matrix = build_permission_matrix()
    assert matrix["elder"]["delete_circle"] == "allowed"
    assert matrix["family"]["edit_medications"] == "grantable_by_elder"
    assert matrix["caretaker"]["view_medications"].startswith("allowed_with_consent:")

    # Permissions cross-circle, family grant, consent, and elder-only failures
    fam = ActorContext(
        session_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        circle_id=uuid.uuid4(),
        role=Role.FAMILY,
        display_name="Fam",
        language="en",
        font_px=18,
        high_contrast=False,
        is_care_contact=False,
        grants=frozenset(),
        consents=frozenset(),
        auth_revision=1,
        circle_timezone="UTC",
        ai_consent_granted=False,
    )
    with pytest.raises(AuthorizationError):
        authorize(fam, Action.VIEW_MEDICATIONS, target_circle_id=uuid.uuid4())
    with pytest.raises(ConsentRequiredError):
        authorize(fam, Action.VIEW_MEDICATIONS, target_circle_id=fam.circle_id)
    with pytest.raises(AuthorizationError):
        authorize(fam, Action.EDIT_MEDICATIONS, target_circle_id=fam.circle_id)
    with pytest.raises(AuthorizationError):
        authorize(fam, Action.MANAGE_MEMBERS, target_circle_id=fam.circle_id)

    # Security passcode byte boundary & token rejection
    with pytest.raises(ValidationError):
        hash_passcode("a" * (MAX_PASSCODE_UTF8_BYTES + 1), rounds=4)
    with pytest.raises(ValidationError):
        hash_passcode("short", rounds=4)

    tok, _ = sign_action_token(
        key_id="v2",
        signing_key="test-primary-hmac-key-with-at-least-32-chars",
        action=ActionTokenType.DOSE_TAKEN,
        circle_id=uuid.uuid4(),
        resource_type="dose",
        resource_id=uuid.uuid4(),
        recipient_membership_id=uuid.uuid4(),
        expires_at=fixed_dt + timedelta(hours=1),
    )
    with pytest.raises(TokenInvalidError):
        verify_action_token(
            tok + "tamper",
            now=fixed_dt,
            active_key_id="v2",
            active_key="test-primary-hmac-key-with-at-least-32-chars",
        )
    with pytest.raises(TokenInvalidError):
        verify_action_token(
            "bad-token",
            now=fixed_dt,
            active_key_id="v2",
            active_key="test-primary-hmac-key-with-at-least-32-chars",
        )
    with pytest.raises(TokenInvalidError):
        verify_action_token(
            tok,
            now=fixed_dt + timedelta(hours=2),
            active_key_id="v2",
            active_key="test-primary-hmac-key-with-at-least-32-chars",
        )


@pytest.mark.integration
def test_medication_schedule_dose_and_refill_edge_paths(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Cover medication update, archive, stock adjustment, refill forecast, and dose error paths."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")

    # Refill forecast edge cases (unavailable, zero stock, bounded by end_date)
    med_unavail = Medication(
        id=uuid.uuid4(),
        circle_id=elder.circle_id,
        name="Drops",
        dose_text="2 drops",
        start_date=date(2026, 5, 10),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    assert (
        compute_refill_forecast(
            medication=med_unavail, schedule=None, today_local=date(2026, 5, 10)
        ).available
        is False
    )

    med_zero = med_unavail.model_copy(update={"stock_count": 0.0, "stock_units_per_dose": 1.0})
    sched = MedicationSchedule(
        id=uuid.uuid4(),
        circle_id=elder.circle_id,
        medication_id=med_zero.id,
        version=1,
        times_local=(time(8, 0),),
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        start_date=date(2026, 5, 10),
        timezone="UTC",
        created_at=clock.now(),
    )
    rf_zero = compute_refill_forecast(
        medication=med_zero, schedule=sched, today_local=date(2026, 5, 10)
    )
    assert rf_zero.available is True and rf_zero.days_remaining == 0

    med_bounded = med_unavail.model_copy(
        update={
            "stock_count": 50.0,
            "stock_units_per_dose": 1.0,
            "end_date": date(2026, 5, 15),
        }
    )
    rf_bounded = compute_refill_forecast(
        medication=med_bounded, schedule=sched, today_local=date(2026, 5, 10)
    )
    assert rf_bounded.estimated_runout_date == date(2026, 5, 15)

    # Validation errors on create_medication
    for bad_kwargs in (
        {"name": "   ", "dose_text": "5mg", "times_local": [time(8, 0)]},
        {"name": "Med", "dose_text": "5mg", "times_local": []},
        {"name": "Med", "dose_text": "5mg", "times_local": [time(8, 0)], "weekdays": [9]},
        {"name": "Med", "dose_text": "5mg", "times_local": [time(8, 0)], "stock_count": -1.0},
        {"name": "Med", "dose_text": "5mg", "times_local": [time(8, 0)], "stock_count": math.nan},
        {
            "name": "Med",
            "dose_text": "5mg",
            "times_local": [time(8, 0)],
            "end_date": date(2026, 5, 1),
        },
    ):
        with pytest.raises(ValidationError):
            container.medication.create_medication(
                elder,
                start_date=date(2026, 5, 10),
                **bad_kwargs,  # type: ignore[arg-type]
            )

    med, _ = container.medication.create_medication(
        elder,
        name="Metformin",
        dose_text="500 mg",
        instructions="After food",
        start_date=date(2026, 5, 10),
        criticality=Criticality.NORMAL,
        stock_count=2.0,
        stock_unit="tabs",
        refill_threshold=2.0,
        stock_units_per_dose=1.0,
        times_local=[time(8, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    assert len(container.medication.list_medications(elder)) == 1

    # Adjust stock above threshold resets low_stock_alert_active
    restocked = container.medication.adjust_stock(
        elder, medication_id=med.id, new_stock_count=20.0, reason="pharmacy refill"
    )
    assert restocked.stock_count == 20.0
    assert restocked.low_stock_alert_active is False

    # Update medication & schedule bumps version and cancels future doses
    upd_med, upd_sched = container.medication.update_medication_schedule(
        elder,
        medication_id=med.id,
        name="Metformin XR",
        dose_text="500 mg ER",
        instructions="With dinner",
        criticality=Criticality.CRITICAL,
        times_local=[time(20, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    assert upd_med.name == "Metformin XR"
    assert upd_sched is not None and upd_sched.version == 2

    with pytest.raises(NotFoundError):
        container.medication.update_medication_schedule(elder, medication_id=uuid.uuid4(), name="X")

    doses = [
        d
        for d in container.dose.list_doses_in_window(
            elder,
            start_utc=clock.now() - timedelta(hours=1),
            end_utc=clock.now() + timedelta(hours=48),
        )
        if d.state == DoseState.PENDING
    ]
    assert len(doses) >= 1
    d0 = doses[0]
    with pytest.raises(ValidationError):
        container.dose.snooze_dose(elder, dose_id=d0.id, minutes=500)
    container.dose.mark_taken(elder, dose_id=d0.id)
    events = container.dose.list_dose_events(elder, dose_id=d0.id)
    assert len(events) >= 1

    with pytest.raises(NotFoundError):
        container.dose.correct_dose(
            elder, dose_id=uuid.uuid4(), new_state=DoseState.SKIPPED, reason="Missing"
        )

    # Archive medication cancels future pending doses while keeping archived view optional
    archived = container.medication.archive_medication(elder, medication_id=med.id)
    assert archived.archived_at is not None
    assert len(container.medication.list_medications(elder, include_archived=False)) == 0
    assert len(container.medication.list_medications(elder, include_archived=True)) == 1


@pytest.mark.integration
def test_circle_checkin_appointment_and_action_token_edges(
    container: ServiceContainer,
    clock: FrozenClock,
    email_gw: FakeEmailGateway,
    tg_gw: FakeTelegramGateway,
) -> None:
    """Cover circle grants/members/audit, checkin escalations/streaks, appointments, and all token verbs."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    _, fam, fam_mem_id = invite_and_join(
        container, elder, email="f@example.test", name="Meera", role=Role.FAMILY
    )

    # Circle members redaction for non-elder + grants + audit + policy options
    mems_for_fam = container.circle.list_members(fam)
    assert all(acc.email_normalized == "***" for _, acc, _ in mems_for_fam)
    updated_fam = container.circle.update_member_grants(
        elder,
        membership_id=fam_mem_id,
        grants=frozenset({FamilyGrant.ADD_OWN_NOTE}),
        is_care_contact=True,
    )
    assert FamilyGrant.ADD_OWN_NOTE in updated_fam.grants

    container.circle.update_policies(
        elder,
        timezone="Asia/Kolkata",
        quiet_hours_enabled=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        elder_quiet_reminders_enabled=False,
        checkin_enabled=True,
        checkin_time=time(9, 0),
        checkin_window_minutes=120,
        primary_contact_membership_id=care_mem_id,
        secondary_contact_membership_id=fam_mem_id,
        checkin_fallback_membership_id=fam_mem_id,
    )
    container.circle.update_policies(
        elder, clear_secondary_contact=True, clear_checkin_fallback=True
    )
    with pytest.raises(ValidationError):
        container.circle.update_policies(elder, checkin_window_minutes=5)
    assert len(container.circle.list_audit_log(elder)) >= 1

    # Invitation errors: invalid, expired, bad display name
    with pytest.raises(TokenInvalidError):
        container.circle.preview_invitation("nonexistent")
    inv_tok = container.circle.create_invitation(
        elder, invited_email="new@example.test", role=Role.FAMILY
    )
    with pytest.raises(ValidationError):
        container.circle.accept_invitation(
            raw_token=inv_tok, passcode="valid-pass-123", display_name=" "
        )
    clock.advance(hours=73)
    with pytest.raises(TokenInvalidError):
        container.circle.preview_invitation(inv_tok)
    with pytest.raises(TokenInvalidError):
        container.circle.accept_invitation(
            raw_token=inv_tok, passcode="valid-pass-123", display_name="X"
        )

    # Pure checkin action & appointment reminder windows
    now = clock.now()
    c_policy = CheckInPolicy(
        timezone="UTC",
        window_minutes=120,
        quiet_hours_enabled=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        elder_quiet_reminders_enabled=False,
    )
    # During quiet hours with elder_quiet_reminders_enabled=False -> waits
    quiet_now = datetime(2026, 5, 10, 23, 0, tzinfo=UTC)
    act_q = next_checkin_action(
        due_at=quiet_now - timedelta(hours=1),
        window_ends_at=quiet_now + timedelta(hours=1),
        expires_at=quiet_now + timedelta(hours=20),
        now=quiet_now,
        responded=False,
        mood=None,
        sent_levels=EscalationProgress(),
        policy=c_policy,
        acknowledged=False,
    )
    assert act_q.kind == "none"

    # Past window -> level 2 contact escalation
    day_due = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
    act_l2 = next_checkin_action(
        due_at=day_due,
        window_ends_at=day_due + timedelta(hours=2),
        expires_at=day_due + timedelta(hours=24),
        now=day_due + timedelta(hours=3),
        responded=False,
        mood=None,
        sent_levels=EscalationProgress(),
        policy=c_policy,
        acknowledged=False,
    )
    assert act_l2.level == 2
    assert act_l2.next_progress is not None
    assert act_l2.next_progress.dispositions[1] == LevelDisposition.SUPERSEDED

    assert select_appointment_reminder_level(starts_at=now, now=now + timedelta(minutes=1)) is None
    assert select_appointment_reminder_level(starts_at=now + timedelta(minutes=45), now=now) == 2
    assert select_appointment_reminder_level(starts_at=now + timedelta(hours=12), now=now) == 1
    assert select_appointment_reminder_level(starts_at=now + timedelta(hours=30), now=now) is None

    # Appointment CRUD + validation errors
    with pytest.raises(ValidationError):
        container.appointment.create_appointment(
            elder, doctor_name="  ", starts_at=now + timedelta(hours=5)
        )
    appt = container.appointment.create_appointment(
        elder, doctor_name="Dr. Rao", starts_at=now + timedelta(hours=5)
    )
    assert appt.doctor_name == "Dr. Rao"
    assert len(container.appointment.list_appointments(elder)) == 1
    with pytest.raises(NotFoundError):
        container.appointment.update_appointment(
            elder, appointment_id=uuid.uuid4(), doctor_name="Dr. X"
        )

    # Check-in & action token redemption across DOSE_SNOOZE, DOSE_CANT_TAKE, CHECKIN_*, CHECKIN_ACK
    ci = container.checkin.get_today_checkin(elder)
    assert ci is not None
    cis, streak = container.checkin.list_checkins(elder)
    assert len(cis) >= 1 and streak == 0

    tok_ci_ok = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.CHECKIN_OKAY,
        circle_id=elder.circle_id,
        resource_type="checkin",
        resource_id=ci.id,
        recipient_membership_id=elder.membership_id,
        window_ends_at=ci.expires_at,
    )
    prev_ci = container.action_tokens.preview_token(tok_ci_ok)
    assert prev_ci.resource_type == "checkin"
    res_ci = container.action_tokens.redeem_token(tok_ci_ok)
    assert res_ci["mood"] == "okay"

    tok_ci_ack = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.CHECKIN_ACK,
        circle_id=elder.circle_id,
        resource_type="checkin",
        resource_id=ci.id,
        recipient_membership_id=care_mem_id,
        window_ends_at=ci.expires_at,
    )
    res_ack = container.action_tokens.redeem_token(tok_ci_ack)
    assert res_ack["first"] is True

    # Dose snooze & cant_take tokens + validation
    container.medication.create_medication(
        elder,
        name="Atorvastatin",
        dose_text="10 mg",
        start_date=clock.now().date(),
        times_local=[time(20, 0)],
    )
    dose = container.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=48)
    )[0]
    tok_snz = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.DOSE_SNOOZE,
        circle_id=elder.circle_id,
        resource_type="dose",
        resource_id=dose.id,
        recipient_membership_id=elder.membership_id,
        window_ends_at=dose.expires_at,
    )
    assert container.action_tokens.redeem_token(tok_snz)["dose_state"] == "snoozed"

    tok_ct = container.action_tokens.issue_token_in_memory(
        action=ActionTokenType.DOSE_CANT_TAKE,
        circle_id=elder.circle_id,
        resource_type="dose",
        resource_id=dose.id,
        recipient_membership_id=elder.membership_id,
        window_ends_at=dose.expires_at,
    )
    with pytest.raises(ValidationError):
        container.action_tokens.redeem_token(tok_ct)
    with pytest.raises(ValidationError):
        container.action_tokens.redeem_token(
            tok_ct, cant_take_reason=CantTakeReason.OTHER, cant_take_note=""
        )
    assert (
        container.action_tokens.redeem_token(
            tok_ct, cant_take_reason=CantTakeReason.UNWELL, cant_take_note="Nauseous"
        )["dose_state"]
        == "can't_take"
    )

    # Notification retry exhaustion (MAX_ATTEMPTS -> FAILED), Telegram Hindi send, & reconcile retryable
    container.auth.update_own_preferences(care, language="hi", font_px=20, high_contrast=False)
    container.auth.update_own_telegram(care, opt_in=True, chat_id="tg-999")
    email_gw.next_state = email_gw.next_state.DEFINITIVE_FAILURE
    tg_gw.next_state = tg_gw.next_state.ACCEPTED

    out_item = NotificationOutboxItem(
        id=uuid.uuid4(),
        circle_id=elder.circle_id,
        logical_key=f"retry-exhaust:{uuid.uuid4()}",
        event_type="urgent_help_alert",
        resource_type="urgent_help",
        resource_id=uuid.uuid4(),
        escalation_level=4,
        recipient_membership_id=care_mem_id,
        channel=NotificationChannel.EMAIL,
        required_category=None,
        resource_version=1,
        status=OutboxStatus.PENDING,
        next_attempt_at=clock.now(),
        payload={
            "elder_name": "Asha",
            "triggered_at_iso": clock.now().isoformat(),
            "timezone": "UTC",
        },
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    container.repos.outbox.enqueue_outbox(out_item)
    for _ in range(4):
        container.notifications.process_due_outbox(worker_id="w-retry")
        clock.advance(minutes=30)
    final_item = container.repos.outbox.get_outbox_by_logical_key(out_item.logical_key)
    assert final_item is not None and final_item.status == OutboxStatus.FAILED

    # Telegram send works cleanly
    tg_item = out_item.model_copy(
        update={
            "id": uuid.uuid4(),
            "logical_key": f"tg-send:{uuid.uuid4()}",
            "channel": NotificationChannel.TELEGRAM,
            "next_attempt_at": clock.now(),
        }
    )
    container.repos.outbox.enqueue_outbox(tg_item)
    res_tg = container.notifications.process_due_outbox(worker_id="w-tg")
    assert res_tg["sent"] == 1
    assert len(tg_gw.sent) >= 1

    # Reconcile uncertain -> retryable and NotFoundError
    with pytest.raises(NotFoundError):
        container.notifications.reconcile_uncertain(elder, item_id=uuid.uuid4(), resolution="sent")

    unc_item = out_item.model_copy(
        update={
            "id": uuid.uuid4(),
            "logical_key": f"unc:{uuid.uuid4()}",
            "status": OutboxStatus.UNCERTAIN,
        }
    )
    container.repos.outbox.enqueue_outbox(unc_item)
    rec_retry = container.notifications.reconcile_uncertain(
        elder, item_id=unc_item.id, resolution="retryable"
    )
    assert rec_retry.status == OutboxStatus.RETRYABLE

    # Worker cycle runs cleanly end-to-end and updates worker health
    health_summary = container.worker.run_cycle()
    assert "outbox_sent" in health_summary
    wh = container.repos.outbox.get_worker_health("reminders")
    assert wh.last_error_category == ""
    assert (
        compute_checkin_streak(
            [
                CheckInInstance(
                    id=uuid.uuid4(),
                    circle_id=elder.circle_id,
                    local_date=date(2026, 5, 1),
                    due_at=clock.now(),
                    window_ends_at=clock.now(),
                    expires_at=clock.now(),
                    mood=CheckInMood.GOOD,
                    responded_at=clock.now(),
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
            ],
            enabled_from_local=date(2026, 5, 1),
            today_local=date(2026, 5, 3),
            current_day_window_closed=False,
        )
        == 1
    )


@pytest.mark.unit
def test_worker_error_handling_and_demo_container_seed(
    email_gw: FakeEmailGateway, clock: FrozenClock
) -> None:
    """Verify WorkerService records error category in WorkerHealth, missing contacts audit, & demo seed."""
    cfg = make_test_config()
    c = build_container(cfg, seed_demo=True)
    circles = c.repos.circle.list_active_circles()
    assert len(circles) == 2

    # Dose escalation without configured contacts writes audit instead of failing
    c2 = build_container(cfg, clock=clock, email_gateway=email_gw, seed_demo=False)
    _, elder = onboard_elder(c2, email="solo@example.test", name="Solo", timezone="UTC")
    c2.medication.create_medication(
        elder,
        name="SoloMed",
        dose_text="1 tab",
        start_date=clock.now().date(),
        times_local=[time(8, 0)],
    )
    dose = c2.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=1), end_utc=clock.now() + timedelta(hours=48)
    )[0]
    circle_obj = c2.circle.get_circle(elder)
    assert (
        c2.notifications.queue_dose_escalation(
            circle=circle_obj, dose=dose, level=2, target_role="primary_contact"
        )
        == []
    )
    assert (
        c2.notifications.queue_and_dispatch_cant_take(circle=circle_obj, dose=dose)
        == "no_contact_configured"
    )
    assert (
        c2.notifications.queue_and_dispatch_urgent_help(
            circle=circle_obj, event_id=uuid.uuid4(), triggered_at=clock.now()
        )
        == "no_contact_configured"
    )

    # Elder level-1 dose and check-in notifications dispatch with signed action links
    c2.worker.run_cycle()
    assert len(email_gw.sent) >= 1

    # Invalid timezone & auth edge validations
    with pytest.raises(ValidationError):
        c2.auth.start_elder_onboarding(
            email="bad@example.test",
            display_name="",
            passcode="valid-pass-123",
            timezone="UTC",
        )
    with pytest.raises(ValidationError):
        c2.auth.start_elder_onboarding(
            email="bad2@example.test",
            display_name="Valid",
            passcode="valid-pass-123",
            timezone="Mars/Olympus",
        )

    # Force worker exception to exercise failure health-recording path
    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated worker boom")

    c.schedule.reconcile_all_circles = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        c.worker.run_cycle()
    wh = c.repos.outbox.get_worker_health("reminders")
    assert wh.last_error_category == "RuntimeError"


@pytest.mark.integration
def test_care_chain_worker_escalations_auth_and_brief_branches(
    email_gw: FakeEmailGateway, clock: FrozenClock
) -> None:
    """Cover level 1/2/3 escalations, low-stock & checkin rendering, eligibility cancels, and auth/brief branches."""
    from services.adherence_service import get_authorized_adherence

    cfg = make_test_config()
    c = build_container(cfg, clock=clock, email_gateway=email_gw, seed_demo=False)
    sess_tok, elder = onboard_elder(c, email="elder_w@example.test", name="Kamala", timezone="UTC")
    _, care, care_mem_id = invite_and_join(
        c, elder, email="care_w@example.test", name="Rohan", role=Role.CARETAKER
    )
    _, fam, fam_mem_id = invite_and_join(
        c, elder, email="fam_w@example.test", name="Meera", role=Role.FAMILY
    )
    c.circle.update_policies(
        elder,
        primary_contact_membership_id=care_mem_id,
        secondary_contact_membership_id=fam_mem_id,
        checkin_enabled=True,
        checkin_time=time(9, 0),
        checkin_window_minutes=60,
    )

    # Create CRITICAL medication with stock at refill threshold to enqueue low-stock alert
    med, _ = c.medication.create_medication(
        elder,
        name="Warfarin",
        dose_text="5 mg",
        start_date=clock.now().date(),
        criticality=Criticality.CRITICAL,
        stock_count=2.0,
        stock_unit="tabs",
        refill_threshold=2.0,
        stock_units_per_dose=1.0,
        times_local=[time(8, 0)],
    )
    # Step 1 (08:00): elder level-1 dose reminder + low_stock_alert
    stats1 = c.worker.run_cycle()
    assert stats1["outbox_sent"] >= 1
    assert any("Warfarin" in m.subject for m in email_gw.sent)

    # Step 2 (+25m -> 08:25): CRITICAL dose reaches level-3 primary_contact escalation
    clock.set(datetime(2026, 5, 10, 8, 25, tzinfo=UTC))
    stats2 = c.worker.run_cycle()
    assert stats2["dose_escalations_queued"] >= 1
    assert any(m.to_address == "care_w@example.test" for m in email_gw.sent)

    # Step 3 (+45m -> 08:45): CRITICAL dose reaches level-4 secondary_contact escalation
    clock.set(datetime(2026, 5, 10, 8, 45, tzinfo=UTC))
    stats3 = c.worker.run_cycle()
    assert stats3["dose_escalations_queued"] >= 1
    assert any(m.to_address == "fam_w@example.test" for m in email_gw.sent)

    # Step 4 (09:15 then 10:16): check-in level-1 elder reminder then level-2 primary_contact escalation
    clock.set(datetime(2026, 5, 10, 9, 15, tzinfo=UTC))
    stats4 = c.worker.run_cycle()
    assert stats4["checkin_escalations_queued"] >= 1
    clock.set(datetime(2026, 5, 10, 10, 16, tzinfo=UTC))
    stats5 = c.worker.run_cycle()
    assert stats5["checkin_escalations_queued"] >= 1

    # Exercise _is_still_eligible cancellation branches (dose taken, checkin responded, appt cancelled)
    dose = c.dose.list_doses_in_window(
        elder, start_utc=clock.now() - timedelta(hours=4), end_utc=clock.now() + timedelta(hours=24)
    )[0]
    taken_res = c.dose.mark_taken(elder, dose_id=dose.id)
    stale_dose_item = NotificationOutboxItem(
        id=uuid.uuid4(),
        circle_id=elder.circle_id,
        logical_key=f"stale-dose:{uuid.uuid4()}",
        event_type="dose_escalation_lvl2",
        resource_type="dose",
        resource_id=dose.id,
        escalation_level=2,
        recipient_membership_id=care_mem_id,
        channel=NotificationChannel.EMAIL,
        required_category=ConsentCategory.MEDICATIONS,
        status=OutboxStatus.PENDING,
        next_attempt_at=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    c.repos.outbox.enqueue_outbox(stale_dose_item)

    ci, _ = c.checkin.submit_checkin(elder, mood=CheckInMood.GOOD)
    stale_ci_item = stale_dose_item.model_copy(
        update={
            "id": uuid.uuid4(),
            "logical_key": f"stale-ci:{uuid.uuid4()}",
            "event_type": "checkin_unanswered_lvl1",
            "resource_type": "checkin",
            "resource_id": ci.id,
            "required_category": ConsentCategory.CHECKINS,
        }
    )
    c.repos.outbox.enqueue_outbox(stale_ci_item)

    appt = c.appointment.create_appointment(
        elder, doctor_name="Dr. Sen", starts_at=clock.now() + timedelta(hours=4)
    )
    c.appointment.cancel_appointment(elder, appointment_id=appt.id)
    stale_appt_item = stale_dose_item.model_copy(
        update={
            "id": uuid.uuid4(),
            "logical_key": f"stale-appt:{uuid.uuid4()}",
            "event_type": "appointment_reminder",
            "resource_type": "appointment",
            "resource_id": appt.id,
            "required_category": ConsentCategory.APPOINTMENTS,
        }
    )
    c.repos.outbox.enqueue_outbox(stale_appt_item)

    cancel_stats = c.notifications.process_due_outbox(worker_id="w-cancel")
    assert cancel_stats["cancelled"] >= 3

    # Authorized adherence helper
    adh = get_authorized_adherence(
        elder,
        doses=[taken_res.dose],
        window_days=7,
        grace_minutes=cfg.adherence_grace_minutes,
        now=clock.now(),
    )
    assert adh.taken == 1

    # Brief & note edge validation errors + list_notes / list_briefs
    with pytest.raises(ValidationError):
        c.brief.add_note(elder, note_type="elder_question", content="   ")
    with pytest.raises(NotFoundError):
        c.brief.edit_note(elder, note_id=uuid.uuid4(), content="Updated")
    with pytest.raises(NotFoundError):
        c.brief.delete_note(elder, note_id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        c.brief.approve_brief(
            elder,
            brief_id=uuid.uuid4(),
            expected_content_hash="abc",
            recipient_membership_ids=[care_mem_id],
        )
    note_ok = c.brief.add_note(elder, note_type="elder_question", content="Can I take after tea?")
    c.brief.edit_note(elder, note_id=note_ok.id, content="Can I take after dinner?")
    assert len(c.brief.list_notes(elder)) >= 1
    brief_gen = c.brief.generate_brief(elder, period_days=7, use_ai_narrative=False)
    assert len(c.brief.list_accessible_briefs(elder)) >= 1
    assert brief_gen.period_days == 7

    # Auth edge validation errors (short passcode, bad telegram opt-in, keep current session on passcode change)
    with pytest.raises(ValidationError):
        c.auth.start_elder_onboarding(
            email="short@example.test", display_name="Short", passcode="123", timezone="UTC"
        )
    with pytest.raises(TokenInvalidError):
        c.auth.preview_verification("does-not-exist")
    with pytest.raises(ValidationError):
        c.auth.update_own_telegram(elder, opt_in=True, chat_id="  ")
    c.auth.change_own_passcode(
        elder,
        current_passcode="elder-pass-123",
        new_passcode="elder-pass-456",
        current_raw_session_token=sess_tok,
    )
    assert c.auth.resolve_session(sess_tok) is not None
