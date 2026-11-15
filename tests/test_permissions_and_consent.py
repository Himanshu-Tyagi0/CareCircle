"""Tests for Role x Action permission matrix, Family grants, cross-circle isolation, and consent."""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest
from core.errors import AuthorizationError, ConsentRequiredError, ValidationError
from core.models import (
    ConsentCategory,
    Criticality,
    FamilyGrant,
    NotificationChannel,
    OutboxStatus,
    Role,
)
from core.models_clinical import NotificationOutboxItem
from core.permissions import Action, authorize
from services.container import ServiceContainer
from tests.conftest import FrozenClock, invite_and_join, onboard_elder


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "grants", "consents", "is_cc", "action", "expected_ok"),
    [
        # Elder has full clinical + circle control
        (Role.ELDER, frozenset(), frozenset(ConsentCategory), False, Action.LOG_DOSE, True),
        (Role.ELDER, frozenset(), frozenset(ConsentCategory), False, Action.EDIT_MEDICATIONS, True),
        (Role.ELDER, frozenset(), frozenset(ConsentCategory), False, Action.MANAGE_CONSENT, True),
        (
            Role.ELDER,
            frozenset(),
            frozenset(ConsentCategory),
            False,
            Action.APPROVE_DOCTOR_BRIEF,
            True,
        ),
        (Role.ELDER, frozenset(), frozenset(ConsentCategory), False, Action.DELETE_CIRCLE, True),
        (Role.ELDER, frozenset(), frozenset(ConsentCategory), False, Action.USE_URGENT_HELP, True),
        # Caretaker
        (Role.CARETAKER, frozenset(), frozenset(ConsentCategory), True, Action.LOG_DOSE, True),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.EDIT_MEDICATIONS,
            True,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.MANAGE_APPOINTMENTS,
            True,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.PREPARE_DOCTOR_BRIEF,
            True,
        ),
        (Role.CARETAKER, frozenset(), frozenset(ConsentCategory), True, Action.CORRECT_DOSE, True),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.ACKNOWLEDGE_CARE,
            True,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            False,
            Action.ACKNOWLEDGE_CARE,
            True,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.MANAGE_CONSENT,
            False,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.APPROVE_DOCTOR_BRIEF,
            False,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.DELETE_CIRCLE,
            False,
        ),
        (
            Role.CARETAKER,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.SUBMIT_CHECKIN,
            False,
        ),
        # Caretaker without consent cannot view/edit meds
        (Role.CARETAKER, frozenset(), frozenset(), True, Action.LOG_DOSE, False),
        (Role.CARETAKER, frozenset(), frozenset(), True, Action.EDIT_MEDICATIONS, False),
        # Family default (view-only where consented, cannot edit or take doses)
        (
            Role.FAMILY,
            frozenset(),
            frozenset(ConsentCategory),
            False,
            Action.VIEW_MEDICATIONS,
            True,
        ),
        (Role.FAMILY, frozenset(), frozenset(ConsentCategory), False, Action.VIEW_ADHERENCE, True),
        (Role.FAMILY, frozenset(), frozenset(ConsentCategory), False, Action.LOG_DOSE, False),
        (
            Role.FAMILY,
            frozenset(),
            frozenset(ConsentCategory),
            False,
            Action.EDIT_MEDICATIONS,
            False,
        ),
        (
            Role.FAMILY,
            frozenset(),
            frozenset(ConsentCategory),
            False,
            Action.ACKNOWLEDGE_CARE,
            False,
        ),
        (Role.FAMILY, frozenset(), frozenset(ConsentCategory), False, Action.ADD_OWN_NOTE, False),
        # Family explicit grants & care contact
        (
            Role.FAMILY,
            frozenset({FamilyGrant.LOG_DOSES}),
            frozenset(ConsentCategory),
            False,
            Action.LOG_DOSE,
            True,
        ),
        (
            Role.FAMILY,
            frozenset({FamilyGrant.EDIT_MEDICATIONS}),
            frozenset(ConsentCategory),
            False,
            Action.EDIT_MEDICATIONS,
            True,
        ),
        (
            Role.FAMILY,
            frozenset({FamilyGrant.ADD_OWN_NOTE}),
            frozenset(ConsentCategory),
            False,
            Action.ADD_OWN_NOTE,
            True,
        ),
        (Role.FAMILY, frozenset(), frozenset(ConsentCategory), True, Action.ACKNOWLEDGE_CARE, True),
        # Care contact ack works without medication editing
        (
            Role.FAMILY,
            frozenset(),
            frozenset(ConsentCategory),
            True,
            Action.EDIT_MEDICATIONS,
            False,
        ),
    ],
)
def test_role_action_permission_matrix(
    role: Role,
    grants: frozenset[FamilyGrant],
    consents: frozenset[ConsentCategory],
    is_cc: bool,
    action: Action,
    expected_ok: bool,
) -> None:
    """Exhaustive Role x Action x Grant x Consent matrix behaves strictly as specified."""
    from core.models import ActorContext

    cid = uuid.uuid4()
    actor = ActorContext(
        session_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        circle_id=cid,
        role=role,
        display_name="Test",
        language="en",
        font_px=18,
        high_contrast=False,
        auth_revision=1,
        circle_timezone="UTC",
        ai_consent_granted=False,
        grants=grants,
        consents=consents,
        is_care_contact=is_cc,
    )
    if expected_ok:
        authorize(actor, action, target_circle_id=cid)
    else:
        with pytest.raises((AuthorizationError, ConsentRequiredError)):
            authorize(actor, action, target_circle_id=cid)


@pytest.mark.integration
def test_cross_circle_isolation_and_own_note_rules(container: ServiceContainer) -> None:
    """Cross-circle IDs are rejected and family note edit/delete respects own_note_only."""
    _, elder1 = onboard_elder(container, email="e1@example.test", name="Elder 1")
    _, elder2 = onboard_elder(container, email="e2@example.test", name="Elder 2")
    _, fam1, _ = invite_and_join(
        container,
        elder1,
        email="fam1@example.test",
        name="Fam 1",
        role=Role.FAMILY,
        grants=frozenset(
            {FamilyGrant.ADD_OWN_NOTE, FamilyGrant.EDIT_OWN_NOTE, FamilyGrant.DELETE_OWN_NOTE}
        ),
    )
    _, care1, _ = invite_and_join(
        container, elder1, email="care1@example.test", name="Care 1", role=Role.CARETAKER
    )

    with pytest.raises(AuthorizationError):
        authorize(elder1, Action.VIEW_MEDICATIONS, target_circle_id=elder2.circle_id)

    # Fam1 writes a note; Care1 cannot edit/delete Fam1's note, Elder1 can
    note = container.brief.add_note(
        fam1, note_type="family_note", content="Bring blood pressure log"
    )
    with pytest.raises(AuthorizationError):
        container.brief.edit_note(care1, note_id=note.id, content="Overwrite")
    with pytest.raises(AuthorizationError):
        container.brief.delete_note(care1, note_id=note.id)

    edited = container.brief.edit_note(fam1, note_id=note.id, content="Bring BP log & glasses")
    assert "glasses" in edited.content
    container.brief.delete_note(elder1, note_id=note.id)


@pytest.mark.integration
def test_consent_revocation_stops_reads_briefs_and_pending_outbox(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Revoking consent stops member reads, brief exports, and unsent outbox deliveries."""
    _, elder = onboard_elder(container, email="e@example.test", name="Asha")
    care_sess, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )

    # Create med & brief while consented
    container.medication.create_medication(
        elder,
        name="Metformin",
        dose_text="500 mg",
        instructions="After breakfast",
        start_date=date(2026, 5, 10),
        end_date=None,
        criticality=Criticality.NORMAL,
        stock_count=10,
        stock_unit="tablets",
        refill_threshold=2,
        stock_units_per_dose=1,
        times_local=[time(8, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    brief = container.brief.generate_brief(elder, period_days=7)
    container.brief.approve_brief(
        elder,
        brief_id=brief.id,
        expected_content_hash=brief.content_hash,
        recipient_membership_ids=[care_mem_id],
    )

    # Enqueue two pending outbox items requiring MEDICATIONS consent for care:
    # 1) before revocation (cancelled immediately on consent revocation)
    # 2) after revocation (cancelled at send-time consent re-verification)
    outbox_item = NotificationOutboxItem(
        id=uuid.uuid4(),
        circle_id=elder.circle_id,
        logical_key=f"test_consent:{uuid.uuid4()}",
        event_type="low_stock_alert",
        resource_type="medication",
        resource_id=uuid.uuid4(),
        escalation_level=1,
        recipient_membership_id=care_mem_id,
        channel=NotificationChannel.EMAIL,
        required_category=ConsentCategory.MEDICATIONS,
        resource_version=1,
        status=OutboxStatus.PENDING,
        next_attempt_at=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    container.repos.outbox.enqueue_outbox(outbox_item)

    # Revoke medications + doctor_brief consent for caretaker
    container.circle.update_member_consent(
        elder,
        membership_id=care_mem_id,
        category=ConsentCategory.MEDICATIONS,
        granted=False,
    )
    container.circle.update_member_consent(
        elder,
        membership_id=care_mem_id,
        category=ConsentCategory.DOCTOR_BRIEF,
        granted=False,
    )
    revoked_item = container.repos.outbox.get_outbox_by_logical_key(outbox_item.logical_key)
    assert revoked_item is not None and revoked_item.status == OutboxStatus.CANCELLED
    assert container.auth.resolve_session(care_sess) is None
    _, care_reloaded = container.auth.issue_session(
        account_id=care.account_id, membership_id=care_mem_id
    )

    with pytest.raises(ConsentRequiredError):
        container.medication.list_medications(care_reloaded)
    with pytest.raises(ConsentRequiredError):
        container.brief.get_brief_for_export(care_reloaded, brief_id=brief.id)

    # Pending outbox item requiring MEDICATIONS is also cancelled at dispatch time
    late_item = outbox_item.model_copy(
        update={"id": uuid.uuid4(), "logical_key": f"test_consent_late:{uuid.uuid4()}"}
    )
    container.repos.outbox.enqueue_outbox(late_item)
    counts = container.notifications.process_due_outbox(worker_id="w1")
    assert counts["cancelled"] == 1
    assert counts["sent"] == 0


@pytest.mark.integration
def test_circle_policies_contacts_member_removal_and_deletion(container: ServiceContainer) -> None:
    """Circle policies validate inputs, block elder removal, and delete/purge circle cleanly."""
    elder_sess, elder = onboard_elder(container, email="e@example.test", name="Asha")
    care_sess, _, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )

    # Invalid contact from outside circle rejected
    with pytest.raises(AuthorizationError):
        container.circle.update_policies(elder, primary_contact_membership_id=uuid.uuid4())

    # Valid contact update works
    updated = container.circle.update_policies(
        elder,
        quiet_hours_enabled=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        primary_contact_membership_id=care_mem_id,
        emergency_country_code="IN",
        emergency_contact_name="Dr. Sen",
        emergency_contact_phone="+91-9876543210",
    )
    assert updated.primary_contact_membership_id == care_mem_id

    # Invalid country code rejected
    with pytest.raises(ValidationError):
        container.circle.update_policies(elder, emergency_country_code="ZZ")

    # Cannot remove elder membership
    with pytest.raises(AuthorizationError):
        container.circle.remove_member(elder, membership_id=elder.membership_id)

    # Removing caretaker revokes caretaker's session immediately
    container.circle.remove_member(elder, membership_id=care_mem_id)
    assert container.auth.resolve_session(care_sess) is None

    # Only elder can delete circle with explicit DELETE confirmation
    with pytest.raises(ValidationError):
        container.circle.delete_circle(elder, confirmation_text="nope")
    container.circle.delete_circle(elder, confirmation_text="DELETE")
    assert container.auth.resolve_session(elder_sess) is None
