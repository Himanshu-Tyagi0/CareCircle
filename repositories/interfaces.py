"""Repository protocol definitions separating business services from persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Protocol
from uuid import UUID

from core.models import (
    Account,
    ActorContext,
    CareCircle,
    CheckInMood,
    ConsentCategory,
    Criticality,
    DoseState,
    FamilyGrant,
    InvitationRecord,
    MemberConsent,
    Membership,
    OutboxStatus,
    PendingVerification,
    Role,
)
from core.models_clinical import (
    ActionTokenRecord,
    Appointment,
    AuditEvent,
    CareAcknowledgement,
    CareNote,
    CheckInInstance,
    DoctorBriefSnapshot,
    DoseEvent,
    DoseInstance,
    Medication,
    MedicationSchedule,
    NotificationOutboxItem,
    WorkerHealth,
)


class AuthRepository(Protocol):
    """Persistence contract for accounts, verification, invitations, lockout, and sessions."""

    def get_account_by_email(self, email_normalized: str) -> Account | None: ...
    def get_account_by_id(self, account_id: UUID) -> Account | None: ...
    def list_memberships_for_account(
        self, account_id: UUID
    ) -> list[tuple[Membership, CareCircle]]: ...
    def record_login_attempt(
        self,
        *,
        email_normalized: str,
        success: bool,
        now: datetime,
        max_failures: int,
        lockout_minutes: int,
        global_limit_per_min: int,
    ) -> dict[str, Any]: ...
    def create_session(
        self,
        *,
        token_hash: str,
        account_id: UUID,
        membership_id: UUID,
        now: datetime,
        idle_minutes: int,
        absolute_hours: int,
    ) -> ActorContext: ...
    def resolve_session(
        self, *, token_hash: str, now: datetime, idle_minutes: int
    ) -> ActorContext | None: ...
    def revoke_session(self, *, token_hash: str, now: datetime) -> None: ...
    def revoke_sessions_for_account(
        self, *, account_id: UUID, now: datetime, except_token_hash: str | None = None
    ) -> int: ...
    def update_preferences(
        self,
        *,
        account_id: UUID,
        language: str,
        font_px: int,
        high_contrast: bool,
    ) -> Account: ...
    def update_telegram_binding(
        self, *, account_id: UUID, opt_in: bool, chat_id: str | None
    ) -> Account: ...
    def update_passcode_and_revoke(
        self,
        *,
        account_id: UUID,
        new_passcode_hash: str,
        now: datetime,
        keep_session_hash: str | None = None,
    ) -> None: ...
    def create_pending_verification(
        self,
        *,
        token_hash: str,
        email_normalized: str,
        display_name: str,
        passcode_hash: str,
        timezone: str,
        language: str,
        expires_at: datetime,
        now: datetime,
    ) -> PendingVerification: ...
    def get_pending_verification_by_hash(self, token_hash: str) -> PendingVerification | None: ...
    def redeem_verification(self, *, token_hash: str, now: datetime) -> dict[str, Any]: ...
    def create_own_circle(
        self,
        *,
        account_id: UUID,
        display_name: str,
        timezone: str,
        now: datetime,
    ) -> tuple[CareCircle, Membership]: ...
    def create_invitation(
        self,
        *,
        circle_id: UUID,
        token_hash: str,
        invited_email_normalized: str,
        role: Role,
        grants: frozenset[FamilyGrant],
        initial_consents: frozenset[ConsentCategory],
        is_care_contact: bool,
        invited_by_account_id: UUID,
        expires_at: datetime,
        now: datetime,
    ) -> InvitationRecord: ...
    def get_invitation_by_hash(
        self, token_hash: str
    ) -> tuple[InvitationRecord, CareCircle] | None: ...
    def redeem_invitation(
        self,
        *,
        token_hash: str,
        now: datetime,
        passcode_hash: str | None,
        display_name: str | None,
        existing_account_id: UUID | None,
    ) -> dict[str, Any]: ...


class CircleRepository(Protocol):
    """Persistence contract for circle policies, memberships, and consents."""

    def get_circle(self, circle_id: UUID) -> CareCircle | None: ...
    def list_active_circles(self) -> list[CareCircle]: ...
    def list_members(
        self, circle_id: UUID
    ) -> list[tuple[Membership, Account, list[MemberConsent]]]: ...
    def get_membership(self, circle_id: UUID, membership_id: UUID) -> Membership | None: ...
    def get_member_consents(
        self, circle_id: UUID, membership_id: UUID
    ) -> frozenset[ConsentCategory]: ...
    def update_member_consent(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        category: ConsentCategory,
        granted: bool,
        actor_account_id: UUID,
        now: datetime,
    ) -> None: ...
    def update_member_grants(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        grants: frozenset[FamilyGrant],
        is_care_contact: bool,
        actor_account_id: UUID,
        now: datetime,
    ) -> Membership: ...
    def deactivate_member(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        actor_account_id: UUID,
        now: datetime,
    ) -> None: ...
    def update_circle_policies(
        self, *, circle_id: UUID, updates: dict[str, Any], actor_account_id: UUID, now: datetime
    ) -> CareCircle: ...
    def delete_circle_purge(
        self, *, circle_id: UUID, actor_account_id: UUID, now: datetime
    ) -> None: ...


class MedicationRepository(Protocol):
    """Persistence contract for medications and versioned schedules."""

    def list_medications(
        self, circle_id: UUID, *, include_archived: bool = False
    ) -> list[Medication]: ...
    def get_medication(self, circle_id: UUID, medication_id: UUID) -> Medication | None: ...
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
    ) -> tuple[Medication, MedicationSchedule]: ...
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
    ) -> tuple[Medication, MedicationSchedule | None]: ...
    def archive_medication(
        self, *, circle_id: UUID, medication_id: UUID, actor_account_id: UUID, now: datetime
    ) -> Medication: ...
    def adjust_stock(
        self,
        *,
        circle_id: UUID,
        medication_id: UUID,
        new_stock_count: float,
        reason: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> Medication: ...
    def set_low_stock_alert_active(
        self, *, circle_id: UUID, medication_id: UUID, active: bool
    ) -> None: ...
    def list_active_schedules(self, circle_id: UUID) -> list[MedicationSchedule]: ...
    def get_active_schedule(
        self, circle_id: UUID, medication_id: UUID
    ) -> MedicationSchedule | None: ...


class DoseRepository(Protocol):
    """Persistence contract for generated dose occurrences and immutable transitions."""

    def upsert_dose_instances(self, doses: list[DoseInstance]) -> int: ...
    def list_doses_in_window(
        self, circle_id: UUID, start_utc: datetime, end_utc: datetime
    ) -> list[DoseInstance]: ...
    def list_active_pending_doses(self, now: datetime, limit: int = 100) -> list[DoseInstance]: ...
    def get_dose(self, circle_id: UUID, dose_id: UUID) -> DoseInstance | None: ...
    def get_dose_by_id_internal(self, dose_id: UUID) -> DoseInstance | None: ...
    def list_dose_events(self, circle_id: UUID, dose_id: UUID) -> list[DoseEvent]: ...
    def transition_taken(
        self, *, circle_id: UUID, dose_id: UUID, actor_account_id: UUID, now: datetime
    ) -> tuple[DoseInstance, bool, bool]: ...
    def transition_snooze(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        snoozed_until: datetime,
        now: datetime,
    ) -> DoseInstance: ...
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
    ) -> DoseInstance: ...
    def transition_skip(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        reason: str,
        now: datetime,
    ) -> DoseInstance: ...
    def correct_dose(
        self,
        *,
        circle_id: UUID,
        dose_id: UUID,
        actor_account_id: UUID,
        new_state: DoseState,
        reason: str,
        now: datetime,
    ) -> DoseInstance: ...
    def cancel_future_doses_for_medication(
        self, *, circle_id: UUID, medication_id: UUID, from_utc: datetime, now: datetime
    ) -> int: ...
    def expire_overdue_doses(self, *, now: datetime) -> int: ...
    def update_escalation_progress(
        self,
        *,
        dose_id: UUID,
        high_water: int,
        escalation_state: dict[str, Any],
        now: datetime,
    ) -> None: ...


class CollabRepository(Protocol):
    """Persistence contract for appointments, check-ins, acknowledgements, notes, and briefs."""

    def list_appointments(
        self, circle_id: UUID, *, include_cancelled: bool = False
    ) -> list[Appointment]: ...
    def list_upcoming_appointments(
        self, from_utc: datetime, to_utc: datetime
    ) -> list[Appointment]: ...
    def get_appointment(self, circle_id: UUID, appointment_id: UUID) -> Appointment | None: ...
    def create_appointment(
        self,
        *,
        circle_id: UUID,
        doctor_name: str,
        specialty: str,
        location: str,
        starts_at: datetime,
        timezone: str,
        notes: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> Appointment: ...
    def update_appointment(
        self,
        *,
        circle_id: UUID,
        appointment_id: UUID,
        updates: dict[str, Any],
        actor_account_id: UUID,
        now: datetime,
    ) -> Appointment: ...
    def cancel_appointment(
        self, *, circle_id: UUID, appointment_id: UUID, actor_account_id: UUID, now: datetime
    ) -> Appointment: ...
    def get_or_create_checkin(
        self,
        *,
        circle_id: UUID,
        local_date: date,
        due_at: datetime,
        window_ends_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> CheckInInstance: ...
    def get_checkin_by_id(self, checkin_id: UUID) -> CheckInInstance | None: ...
    def list_checkins(
        self, circle_id: UUID, start_date: date, end_date: date
    ) -> list[CheckInInstance]: ...
    def list_active_unresponded_checkins(self, now: datetime) -> list[CheckInInstance]: ...
    def submit_checkin_response(
        self,
        *,
        circle_id: UUID,
        checkin_id: UUID,
        mood: CheckInMood,
        note: str,
        actor_account_id: UUID,
        now: datetime,
    ) -> tuple[CheckInInstance, CheckInMood | None]: ...
    def update_checkin_escalation(
        self,
        *,
        checkin_id: UUID,
        high_water: int,
        escalation_state: dict[str, Any],
        now: datetime,
    ) -> None: ...
    def record_acknowledgement(
        self,
        *,
        circle_id: UUID,
        episode_type: str,
        resource_id: UUID,
        membership_id: UUID,
        actor_name: str,
        now: datetime,
    ) -> tuple[CareAcknowledgement, bool]: ...
    def get_acknowledgement(
        self, *, episode_type: str, resource_id: UUID
    ) -> CareAcknowledgement | None: ...
    def list_acknowledgements(
        self, circle_id: UUID, limit: int = 20
    ) -> list[CareAcknowledgement]: ...
    def list_notes(self, circle_id: UUID) -> list[CareNote]: ...
    def get_note(self, circle_id: UUID, note_id: UUID) -> CareNote | None: ...
    def create_note(
        self,
        *,
        circle_id: UUID,
        author_account_id: UUID,
        author_name: str,
        note_type: str,
        content: str,
        now: datetime,
    ) -> CareNote: ...
    def update_note(
        self, *, circle_id: UUID, note_id: UUID, content: str, now: datetime
    ) -> CareNote: ...
    def delete_note(self, *, circle_id: UUID, note_id: UUID) -> None: ...
    def save_brief(self, brief: DoctorBriefSnapshot) -> DoctorBriefSnapshot: ...
    def get_brief(self, circle_id: UUID, brief_id: UUID) -> DoctorBriefSnapshot | None: ...
    def list_briefs(self, circle_id: UUID) -> list[DoctorBriefSnapshot]: ...
    def approve_brief(
        self,
        *,
        circle_id: UUID,
        brief_id: UUID,
        expected_content_hash: str,
        recipient_membership_ids: tuple[UUID, ...],
        now: datetime,
    ) -> DoctorBriefSnapshot: ...
    def invalidate_brief_approvals(self, *, circle_id: UUID) -> None: ...


class OutboxRepository(Protocol):
    """Persistence contract for action tokens, transactional outbox, audit log, and worker health."""

    def insert_action_token(self, token: ActionTokenRecord) -> None: ...
    def get_action_token_by_hash(self, token_hash: str) -> ActionTokenRecord | None: ...
    def consume_action_token(self, *, token_hash: str, now: datetime) -> bool: ...
    def invalidate_action_tokens_for_resource(self, *, resource_id: UUID, now: datetime) -> int: ...
    def enqueue_outbox(
        self, item: NotificationOutboxItem
    ) -> tuple[NotificationOutboxItem, bool]: ...
    def get_outbox_by_logical_key(self, logical_key: str) -> NotificationOutboxItem | None: ...
    def reactivate_outbox_item(
        self, *, logical_key: str, payload: dict[str, Any], now: datetime
    ) -> NotificationOutboxItem | None: ...
    def list_outbox(self, circle_id: UUID, limit: int = 50) -> list[NotificationOutboxItem]: ...
    def claim_outbox_batch(
        self, *, worker_id: str, now: datetime, lease_seconds: int, batch_size: int
    ) -> list[NotificationOutboxItem]: ...
    def mark_outbox_sending(self, *, item_id: UUID, lease_owner: str, now: datetime) -> bool: ...
    def update_outbox_outcome(
        self,
        *,
        item_id: UUID,
        status: OutboxStatus,
        now: datetime,
        attempts: int | None = None,
        next_attempt_at: datetime | None = None,
        error_code: str = "",
        provider_message_id: str = "",
    ) -> NotificationOutboxItem: ...
    def cancel_outbox_for_resource(
        self,
        *,
        resource_id: UUID,
        event_prefix: str | None = None,
        logical_key: str | None = None,
        now: datetime,
    ) -> int: ...
    def cancel_outbox_for_recipient(
        self,
        *,
        circle_id: UUID,
        recipient_membership_id: UUID,
        category: ConsentCategory | None,
        now: datetime,
    ) -> int: ...
    def count_outbox_metrics(self, now: datetime) -> dict[str, int]: ...
    def append_audit(self, event: AuditEvent) -> None: ...
    def list_audit(self, circle_id: UUID, limit: int = 100) -> list[AuditEvent]: ...
    def get_worker_health(self, worker_name: str = "reminders") -> WorkerHealth: ...
    def save_worker_health(self, health: WorkerHealth) -> None: ...


@dataclass(frozen=True)
class Repositories:
    """Dependency bundle containing all CareCircle repository boundaries."""

    auth: AuthRepository
    circle: CircleRepository
    medication: MedicationRepository
    dose: DoseRepository
    collab: CollabRepository
    outbox: OutboxRepository
