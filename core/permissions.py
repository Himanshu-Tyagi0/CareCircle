"""Centralized RBAC, family-grant, category-consent, and cross-circle permission matrix."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from core.errors import AuthorizationError, ConsentRequiredError
from core.models import ActorContext, ConsentCategory, FamilyGrant, Role


class Action(StrEnum):
    """All protected operations in CareCircle."""

    # Self-service account preferences (all active members)
    UPDATE_OWN_PREFERENCES = "update_own_preferences"
    CHANGE_OWN_PASSCODE = "change_own_passcode"
    CREATE_OWN_CIRCLE = "create_own_circle"

    # Elder-only circle governance
    MANAGE_MEMBERS = "manage_members"
    INVITE_MEMBER = "invite_member"
    MANAGE_CONSENT = "manage_consent"
    MANAGE_FAMILY_GRANTS = "manage_family_grants"
    MANAGE_CONTACT_POLICY = "manage_contact_policy"
    MANAGE_AI_CONSENT = "manage_ai_consent"
    VIEW_AUDIT_LOG = "view_audit_log"
    DELETE_CIRCLE = "delete_circle"
    APPROVE_DOCTOR_BRIEF = "approve_doctor_brief"
    MANAGE_ELDER_QUESTIONS = "manage_elder_questions"
    MANAGE_ALL_NOTES = "manage_all_notes"
    USE_URGENT_HELP = "use_urgent_help"

    # Medications & doses
    VIEW_MEDICATIONS = "view_medications"
    EDIT_MEDICATIONS = "edit_medications"
    LOG_DOSE = "log_dose"
    CORRECT_DOSE = "correct_dose"
    VIEW_ADHERENCE = "view_adherence"

    # Check-ins
    VIEW_CHECKINS = "view_checkins"
    SUBMIT_CHECKIN = "submit_checkin"

    # Appointments
    VIEW_APPOINTMENTS = "view_appointments"
    MANAGE_APPOINTMENTS = "manage_appointments"

    # Doctor brief & notes
    VIEW_DOCTOR_BRIEF = "view_doctor_brief"
    PREPARE_DOCTOR_BRIEF = "prepare_doctor_brief"
    ADD_OWN_NOTE = "add_own_note"
    EDIT_OWN_NOTE = "edit_own_note"
    DELETE_OWN_NOTE = "delete_own_note"

    # Coordination / Care Chain
    ACKNOWLEDGE_CARE = "acknowledge_care"
    RECONCILE_OUTBOX = "reconcile_outbox"
    USE_AI_ASSIST = "use_ai_assist"


ROLE_BASE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.ELDER: frozenset(Action),
    Role.CARETAKER: frozenset(
        {
            Action.UPDATE_OWN_PREFERENCES,
            Action.CHANGE_OWN_PASSCODE,
            Action.CREATE_OWN_CIRCLE,
            Action.VIEW_MEDICATIONS,
            Action.EDIT_MEDICATIONS,
            Action.LOG_DOSE,
            Action.CORRECT_DOSE,
            Action.VIEW_ADHERENCE,
            Action.VIEW_CHECKINS,
            Action.VIEW_APPOINTMENTS,
            Action.MANAGE_APPOINTMENTS,
            Action.VIEW_DOCTOR_BRIEF,
            Action.PREPARE_DOCTOR_BRIEF,
            Action.ADD_OWN_NOTE,
            Action.EDIT_OWN_NOTE,
            Action.DELETE_OWN_NOTE,
            Action.ACKNOWLEDGE_CARE,
            Action.USE_AI_ASSIST,
        }
    ),
    Role.FAMILY: frozenset(
        {
            Action.UPDATE_OWN_PREFERENCES,
            Action.CHANGE_OWN_PASSCODE,
            Action.CREATE_OWN_CIRCLE,
            Action.VIEW_MEDICATIONS,
            Action.VIEW_ADHERENCE,
            Action.VIEW_CHECKINS,
            Action.VIEW_APPOINTMENTS,
            Action.VIEW_DOCTOR_BRIEF,
            Action.USE_AI_ASSIST,
        }
    ),
}

FAMILY_GRANT_TO_ACTION: dict[FamilyGrant, Action] = {
    FamilyGrant.EDIT_MEDICATIONS: Action.EDIT_MEDICATIONS,
    FamilyGrant.LOG_DOSES: Action.LOG_DOSE,
    FamilyGrant.MANAGE_APPOINTMENTS: Action.MANAGE_APPOINTMENTS,
    FamilyGrant.ADD_OWN_NOTE: Action.ADD_OWN_NOTE,
    FamilyGrant.EDIT_OWN_NOTE: Action.EDIT_OWN_NOTE,
    FamilyGrant.DELETE_OWN_NOTE: Action.DELETE_OWN_NOTE,
    FamilyGrant.PREPARE_BRIEF: Action.PREPARE_DOCTOR_BRIEF,
}

ACTION_REQUIRED_CATEGORY: dict[Action, ConsentCategory] = {
    Action.VIEW_MEDICATIONS: ConsentCategory.MEDICATIONS,
    Action.EDIT_MEDICATIONS: ConsentCategory.MEDICATIONS,
    Action.LOG_DOSE: ConsentCategory.MEDICATIONS,
    Action.CORRECT_DOSE: ConsentCategory.MEDICATIONS,
    Action.VIEW_ADHERENCE: ConsentCategory.ADHERENCE,
    Action.VIEW_CHECKINS: ConsentCategory.CHECKINS,
    Action.VIEW_APPOINTMENTS: ConsentCategory.APPOINTMENTS,
    Action.MANAGE_APPOINTMENTS: ConsentCategory.APPOINTMENTS,
    Action.VIEW_DOCTOR_BRIEF: ConsentCategory.DOCTOR_BRIEF,
    Action.PREPARE_DOCTOR_BRIEF: ConsentCategory.DOCTOR_BRIEF,
    Action.ADD_OWN_NOTE: ConsentCategory.DOCTOR_BRIEF,
    Action.EDIT_OWN_NOTE: ConsentCategory.DOCTOR_BRIEF,
    Action.DELETE_OWN_NOTE: ConsentCategory.DOCTOR_BRIEF,
}

OWN_RESOURCE_ACTIONS = frozenset({Action.EDIT_OWN_NOTE, Action.DELETE_OWN_NOTE})


def is_action_allowed_by_role(actor: ActorContext, action: Action) -> bool:
    """Return True if actor's role, grants, or care-contact flag permit the action."""
    if action in ROLE_BASE_ACTIONS.get(actor.role, frozenset()):
        return True
    if actor.role == Role.FAMILY:
        for grant, granted_action in FAMILY_GRANT_TO_ACTION.items():
            if granted_action == action and grant in actor.grants:
                return True
        if action == Action.ACKNOWLEDGE_CARE and actor.is_care_contact:
            return True
    return bool(
        actor.role == Role.CARETAKER and action == Action.ACKNOWLEDGE_CARE and actor.is_care_contact
    )


def authorize(
    actor: ActorContext,
    action: Action,
    *,
    target_circle_id: UUID | None = None,
    required_categories: tuple[ConsentCategory, ...] | None = None,
    author_account_id: UUID | None = None,
) -> None:
    """Enforce circle isolation, role/grant permission, ownership, and category consent."""
    if target_circle_id is not None and target_circle_id != actor.circle_id:
        raise AuthorizationError("errors.cross_circle_denied")

    if not is_action_allowed_by_role(actor, action):
        raise AuthorizationError("errors.forbidden")

    if (
        action in OWN_RESOURCE_ACTIONS
        and actor.role != Role.ELDER
        and (author_account_id is None or author_account_id != actor.account_id)
    ):
        raise AuthorizationError("errors.own_note_only")

    if actor.role != Role.ELDER:
        categories: list[ConsentCategory] = []
        default_cat = ACTION_REQUIRED_CATEGORY.get(action)
        if default_cat is not None:
            categories.append(default_cat)
        if required_categories:
            categories.extend(required_categories)
        for cat in categories:
            if cat not in actor.consents:
                raise ConsentRequiredError(
                    "errors.consent_required", params={"category": cat.value}
                )


def build_permission_matrix() -> dict[str, dict[str, str]]:
    """Return an explicit matrix describing every Role x Action baseline permission."""
    matrix: dict[str, dict[str, str]] = {}
    for role in Role:
        role_row: dict[str, str] = {}
        base = ROLE_BASE_ACTIONS[role]
        for action in Action:
            if action in base:
                cat = ACTION_REQUIRED_CATEGORY.get(action)
                role_row[action.value] = (
                    "allowed"
                    if role == Role.ELDER or cat is None
                    else f"allowed_with_consent:{cat.value}"
                )
            elif role == Role.FAMILY and action in FAMILY_GRANT_TO_ACTION.values():
                role_row[action.value] = "grantable_by_elder"
            elif role == Role.FAMILY and action == Action.ACKNOWLEDGE_CARE:
                role_row[action.value] = "care_contact_only"
            else:
                role_row[action.value] = "denied"
        matrix[role.value] = role_row
    return matrix
