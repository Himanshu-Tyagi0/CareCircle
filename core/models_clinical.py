"""Clinical coordination, schedules, doses, appointments, briefs, outbox, and audit models."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.models import (
    ActionTokenType,
    CantTakeReason,
    CheckInMood,
    ConsentCategory,
    Criticality,
    DoseState,
    NotificationChannel,
    OutboxStatus,
)


class Medication(BaseModel):
    """Medication definition with explicit inventory configuration."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    name: str
    dose_text: str
    instructions: str = ""
    start_date: date
    end_date: date | None = None
    criticality: Criticality = Criticality.NORMAL
    stock_count: float | None = None
    stock_unit: str = ""
    refill_threshold: float | None = None
    stock_units_per_dose: float | None = None
    low_stock_alert_active: bool = False
    archived_at: datetime | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class MedicationSchedule(BaseModel):
    """Versioned schedule specifying local times and active weekdays."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    medication_id: UUID
    version: int
    times_local: tuple[time, ...]
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    start_date: date
    end_date: date | None = None
    timezone: str
    active: bool = True
    created_at: datetime


class DoseInstance(BaseModel):
    """Generated medication dose occurrence anchored to UTC due time and local context."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    medication_id: UUID
    schedule_id: UUID
    schedule_version: int
    medication_name: str
    dose_text: str
    instructions: str = ""
    criticality: Criticality
    due_at: datetime
    local_date: date
    local_time_intended: time
    dst_adjusted: bool = False
    dst_adjustment_note: str = ""
    expires_at: datetime
    state: DoseState = DoseState.PENDING
    snoozed_until: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_by_account_id: UUID | None = None
    cant_take_report_id: UUID | None = None
    cant_take_reason: CantTakeReason | None = None
    cant_take_note: str = ""
    skip_reason: str = ""
    inventory_decremented: bool = False
    inventory_discrepancy: bool = False
    escalation_high_water: int = 0
    escalation_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DoseEvent(BaseModel):
    """Immutable audit/history event for a dose transition or correction."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    dose_id: UUID
    event_type: str
    actor_account_id: UUID | None = None
    previous_state: DoseState | None = None
    new_state: DoseState
    reason: str = ""
    note: str = ""
    stock_delta: float = 0.0
    occurred_at: datetime


class Appointment(BaseModel):
    """Medical appointment with versioned reminder state."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    doctor_name: str
    specialty: str = ""
    location: str = ""
    starts_at: datetime
    timezone: str
    notes: str = ""
    version: int = 1
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CheckInInstance(BaseModel):
    """Daily wellbeing check-in instance per circle and local date."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    local_date: date
    due_at: datetime
    window_ends_at: datetime
    expires_at: datetime
    mood: CheckInMood | None = None
    note: str = ""
    responded_at: datetime | None = None
    responded_by_account_id: UUID | None = None
    escalation_high_water: int = 0
    escalation_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CareAcknowledgement(BaseModel):
    """First-writer-wins contact acknowledgement for a care episode."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    episode_type: str
    resource_id: UUID
    acknowledged_by_membership_id: UUID
    acknowledged_by_name: str
    acknowledged_at: datetime


class CareNote(BaseModel):
    """Bounded factual family note or elder doctor question."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    author_account_id: UUID
    author_name: str
    note_type: str  # "family_note" | "elder_question"
    content: str
    created_at: datetime
    updated_at: datetime


class DoctorBriefSnapshot(BaseModel):
    """Deterministic visit brief with exact content hash and version-bound elder approval."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    period_days: int
    period_start: date
    period_end: date
    timezone: str
    source_categories: frozenset[ConsentCategory]
    sections: dict[str, Any]
    factual_statements: list[dict[str, str]]
    ai_narrative_en: str = ""
    ai_narrative_hi: str = ""
    content_hash: str
    version: int = 1
    approved_by_elder_at: datetime | None = None
    approved_recipient_membership_ids: tuple[UUID, ...] = ()
    created_by_account_id: UUID
    created_at: datetime


class ActionTokenRecord(BaseModel):
    """Hashed single-use expiring action token stored in DB."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    token_hash: str
    key_id: str
    action_type: ActionTokenType
    resource_type: str
    resource_id: UUID
    recipient_membership_id: UUID
    expires_at: datetime
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None
    created_at: datetime


class NotificationOutboxItem(BaseModel):
    """Transactional outbox row with unique logical key and safe metadata."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    logical_key: str
    event_type: str
    resource_type: str
    resource_id: UUID
    escalation_level: int = 0
    recipient_membership_id: UUID
    channel: NotificationChannel
    required_category: ConsentCategory | None = None
    resource_version: int = 1
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    next_attempt_at: datetime
    lease_expires_at: datetime | None = None
    lease_owner: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    last_error_code: str = ""
    provider_message_id: str = ""
    created_at: datetime
    updated_at: datetime


class AuditEvent(BaseModel):
    """Append-only audit record storing safe metadata without secrets or verbose PHI."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    circle_id: UUID
    actor_account_id: UUID | None = None
    actor_membership_id: UUID | None = None
    operation: str
    target_type: str
    target_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class WorkerHealth(BaseModel):
    """Persisted background reminder worker status summary."""

    model_config = ConfigDict(frozen=True)

    worker_name: str = "reminders"
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_category: str = ""
    due_event_lag_seconds: int = 0
    uncertain_send_count: int = 0
    last_summary: dict[str, Any] = Field(default_factory=dict)
