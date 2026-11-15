"""Thread-safe in-memory isolated store mirroring CareCircle PostgreSQL invariants."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from core.models import (
    Account,
    CareCircle,
    ConsentCategory,
    InvitationRecord,
    MemberConsent,
    Membership,
    PendingVerification,
    SessionRecord,
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


@dataclass
class DemoStore:
    """Isolated synthetic storage for demo mode and unit/integration tests."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    accounts: dict[UUID, Account] = field(default_factory=dict)
    email_index: dict[str, UUID] = field(default_factory=dict)
    circles: dict[UUID, CareCircle] = field(default_factory=dict)
    deleted_circle_ids: set[UUID] = field(default_factory=set)
    memberships: dict[UUID, Membership] = field(default_factory=dict)
    consents: dict[tuple[UUID, UUID, ConsentCategory], MemberConsent] = field(default_factory=dict)
    pending_verifications: dict[str, PendingVerification] = field(default_factory=dict)
    invitations: dict[str, InvitationRecord] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    login_buckets: dict[datetime, int] = field(default_factory=dict)
    medications: dict[UUID, Medication] = field(default_factory=dict)
    schedules: dict[UUID, MedicationSchedule] = field(default_factory=dict)
    doses: dict[UUID, DoseInstance] = field(default_factory=dict)
    dose_med_due_index: dict[tuple[UUID, datetime], UUID] = field(default_factory=dict)
    dose_events: list[DoseEvent] = field(default_factory=list)
    appointments: dict[UUID, Appointment] = field(default_factory=dict)
    checkins: dict[UUID, CheckInInstance] = field(default_factory=dict)
    checkin_date_index: dict[tuple[UUID, object], UUID] = field(default_factory=dict)
    acknowledgements: dict[tuple[str, UUID], CareAcknowledgement] = field(default_factory=dict)
    notes: dict[UUID, CareNote] = field(default_factory=dict)
    briefs: dict[UUID, DoctorBriefSnapshot] = field(default_factory=dict)
    action_tokens: dict[str, ActionTokenRecord] = field(default_factory=dict)
    outbox: dict[UUID, NotificationOutboxItem] = field(default_factory=dict)
    outbox_logical_index: dict[str, UUID] = field(default_factory=dict)
    audit_events: list[AuditEvent] = field(default_factory=list)
    worker_health: dict[str, WorkerHealth] = field(
        default_factory=lambda: {"reminders": WorkerHealth()}
    )
