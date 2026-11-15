"""Domain enums and identity, circle, consent, and session models."""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Circle membership roles."""

    ELDER = "elder"
    CARETAKER = "caretaker"
    FAMILY = "family"


class ConsentCategory(StrEnum):
    """Granular categories controlled per-member by the elder."""

    MEDICATIONS = "medications"
    ADHERENCE = "adherence"
    CHECKINS = "checkins"
    APPOINTMENTS = "appointments"
    DOCTOR_BRIEF = "doctor_brief"


class Criticality(StrEnum):
    """User-selected medication criticality levels."""

    CRITICAL = "critical"
    NORMAL = "normal"
    OPTIONAL = "optional"


class DoseState(StrEnum):
    """Dose occurrence states."""

    PENDING = "pending"
    SNOOZED = "snoozed"
    TAKEN = "taken"
    CANT_TAKE = "can't_take"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CantTakeReason(StrEnum):
    """Explicit bounded reasons for reporting inability to take a dose."""

    UNWELL = "unwell"
    NAUSEA_OR_SIDE_EFFECT = "nausea_or_side_effect"
    OUT_OF_STOCK = "out_of_stock"
    DOCTOR_ADVISED_SKIP = "doctor_advised_skip"
    OTHER = "other"


class CheckInMood(StrEnum):
    """Daily wellbeing check-in moods."""

    GOOD = "good"
    OKAY = "okay"
    NOT_WELL = "not_well"


class OutboxStatus(StrEnum):
    """Transactional notification outbox states."""

    PENDING = "pending"
    LEASED = "leased"
    SENDING = "sending"
    SENT = "sent"
    RETRYABLE = "retryable"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class NotificationChannel(StrEnum):
    """Supported notification delivery channels."""

    EMAIL = "email"
    TELEGRAM = "telegram"


class ActionTokenType(StrEnum):
    """Allowlisted signed action-token verbs."""

    DOSE_TAKEN = "dose_taken"
    DOSE_SNOOZE = "dose_snooze"
    DOSE_CANT_TAKE = "dose_cant_take"
    CARE_ACK = "care_ack"
    CHECKIN_GOOD = "checkin_good"
    CHECKIN_OKAY = "checkin_okay"
    CHECKIN_NOT_WELL = "checkin_not_well"
    CHECKIN_ACK = "checkin_ack"


class FamilyGrant(StrEnum):
    """Specific opt-in edit grants an elder can assign individually to a family member."""

    EDIT_MEDICATIONS = "edit_medications"
    LOG_DOSES = "log_doses"
    MANAGE_APPOINTMENTS = "manage_appointments"
    ADD_OWN_NOTE = "add_own_note"
    EDIT_OWN_NOTE = "edit_own_note"
    DELETE_OWN_NOTE = "delete_own_note"
    PREPARE_BRIEF = "prepare_brief"


class AccountStatus(StrEnum):
    """Account lifecycle states."""

    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    DISABLED = "disabled"


class Account(BaseModel):
    """User account profile and preferences."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email_normalized: str
    display_name: str
    passcode_hash: str
    status: AccountStatus = AccountStatus.ACTIVE
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    language: str = "en"
    font_px: int = 24
    high_contrast: bool = False
    telegram_opt_in: bool = False
    telegram_chat_id: str | None = None
    created_at: datetime


class CareCircle(BaseModel):
    """Elder-owned care circle profile and safety/quiet-hour policies."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    elder_account_id: UUID
    display_name: str
    timezone: str = "Asia/Kolkata"
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)
    elder_quiet_reminders_enabled: bool = True
    checkin_enabled: bool = True
    checkin_time: time = time(9, 0)
    checkin_window_minutes: int = 120
    checkin_enabled_from: datetime | None = None
    primary_contact_membership_id: UUID | None = None
    secondary_contact_membership_id: UUID | None = None
    checkin_fallback_membership_id: UUID | None = None
    emergency_country_code: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    ai_consent_granted: bool = False
    ai_consent_updated_at: datetime | None = None
    meal_breakfast_time: time = time(8, 0)
    meal_lunch_time: time = time(13, 0)
    meal_dinner_time: time = time(20, 0)
    created_at: datetime


class Membership(BaseModel):
    """Circle membership linking an account with role, grants, and auth revision."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    account_id: UUID
    role: Role
    active: bool = True
    is_care_contact: bool = False
    grants: frozenset[FamilyGrant] = Field(default_factory=frozenset)
    auth_revision: int = 1
    created_at: datetime


class MemberConsent(BaseModel):
    """Per-member category visibility grant/revocation."""

    model_config = ConfigDict(frozen=True)

    circle_id: UUID
    membership_id: UUID
    category: ConsentCategory
    granted: bool
    updated_at: datetime
    updated_by_account_id: UUID


class SessionRecord(BaseModel):
    """Server-side opaque session binding an authenticated account and single membership."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    token_hash: str
    account_id: UUID
    membership_id: UUID
    circle_id: UUID
    auth_revision: int
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None


class ActorContext(BaseModel):
    """Validated authenticated actor context resolved from a live session."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    account_id: UUID
    membership_id: UUID
    circle_id: UUID
    role: Role
    display_name: str
    language: str
    font_px: int
    high_contrast: bool
    is_care_contact: bool
    grants: frozenset[FamilyGrant] = Field(default_factory=frozenset)
    consents: frozenset[ConsentCategory] = Field(default_factory=frozenset)
    auth_revision: int
    circle_timezone: str
    ai_consent_granted: bool


class PendingVerification(BaseModel):
    """Pending email ownership verification for first-elder onboarding."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    token_hash: str
    email_normalized: str
    display_name: str
    passcode_hash: str
    timezone: str
    language: str
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime


class InvitationRecord(BaseModel):
    """Single-use expiring invitation bound to an intended email and initial consents."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    token_hash: str
    invited_email_normalized: str
    role: Role
    grants: frozenset[FamilyGrant] = Field(default_factory=frozenset)
    initial_consents: frozenset[ConsentCategory] = Field(default_factory=frozenset)
    is_care_contact: bool = False
    invited_by_account_id: UUID
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
