"""Daily wellbeing check-in service, pure check-in escalation policy, streaks, and urgent help."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.clock import Clock
from core.errors import ValidationError
from core.models import ActorContext, CheckInMood
from core.models_clinical import AuditEvent, CheckInInstance
from core.permissions import Action, authorize
from repositories.interfaces import CircleRepository, CollabRepository, OutboxRepository

from services.escalation_service import (
    Action as EscalationAction,
)
from services.escalation_service import (
    EscalationProgress,
    LevelDisposition,
    Policy,
    is_in_quiet_hours,
)
from services.schedule_service import resolve_local_instant


@dataclass(frozen=True)
class CheckInPolicy:
    """Immutable check-in escalation configuration."""

    timezone: str = "Asia/Kolkata"
    window_minutes: int = 120
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)
    elder_quiet_reminders_enabled: bool = True


def next_checkin_action(
    *,
    due_at: datetime,
    window_ends_at: datetime,
    expires_at: datetime,
    now: datetime,
    responded: bool,
    mood: CheckInMood | None,
    sent_levels: EscalationProgress,
    policy: CheckInPolicy,
    acknowledged: bool,
    not_well_report_id: str = "",
) -> EscalationAction:
    """Pure check-in Care Chain policy independent of wall clock."""
    if responded and mood == CheckInMood.NOT_WELL:
        if not_well_report_id and not_well_report_id not in sent_levels.handled_report_ids:
            new_disp = dict(sent_levels.dispositions)
            new_disp[3] = LevelDisposition.QUEUED
            return EscalationAction(
                kind="checkin_not_well",
                level=3,
                target_role="caretaker_or_fallback",
                report_id=not_well_report_id,
                next_progress=EscalationProgress(
                    high_water_level=max(sent_levels.high_water_level, 3),
                    dispositions=new_disp,
                    handled_report_ids=sent_levels.handled_report_ids | {not_well_report_id},
                ),
            )
        return EscalationAction(kind="none", reason="not_well_already_handled")

    if responded or now >= expires_at:
        return EscalationAction(kind="none", reason="responded_or_expired")

    if acknowledged:
        return EscalationAction(kind="none", reason="acknowledged")

    if now < due_at:
        return EscalationAction(kind="none", reason="not_due")

    quiet = is_in_quiet_hours(
        now,
        Policy(
            timezone=policy.timezone,
            quiet_hours_enabled=policy.quiet_hours_enabled,
            quiet_hours_start=policy.quiet_hours_start,
            quiet_hours_end=policy.quiet_hours_end,
            elder_quiet_reminders_enabled=policy.elder_quiet_reminders_enabled,
        ),
    )

    eligible: list[tuple[int, str, str]] = []
    if sent_levels.high_water_level < 1 and not (
        quiet and not policy.elder_quiet_reminders_enabled
    ):
        eligible.append((1, "checkin_elder_reminder", "elder"))
    if now >= window_ends_at and sent_levels.high_water_level < 2 and not quiet:
        eligible.append((2, "checkin_unanswered_contact", "primary_contact"))

    if not eligible:
        return EscalationAction(kind="none", reason="waiting_or_quiet")

    chosen_level, chosen_kind, chosen_role = eligible[-1]
    new_disp = dict(sent_levels.dispositions)
    if sent_levels.high_water_level < 1 < chosen_level and 1 not in new_disp:
        new_disp[1] = LevelDisposition.SUPERSEDED
    new_disp[chosen_level] = LevelDisposition.QUEUED
    return EscalationAction(
        kind=chosen_kind,
        level=chosen_level,
        target_role=chosen_role,
        next_progress=EscalationProgress(
            high_water_level=chosen_level,
            dispositions=new_disp,
            handled_report_ids=sent_levels.handled_report_ids,
        ),
    )


def compute_checkin_streak(
    checkins: list[CheckInInstance],
    *,
    enabled_from_local: date | None,
    today_local: date,
    current_day_window_closed: bool,
) -> int:
    """Compute consecutive check-in response days (any mood counts; unfinished today won't break)."""
    by_date = {c.local_date: c for c in checkins}
    if not by_date:
        return 0
    min_date = enabled_from_local or min(by_date.keys())
    streak = 0

    today_ci = by_date.get(today_local)
    if today_ci and today_ci.responded_at is not None:
        streak += 1
    elif today_ci and current_day_window_closed:
        return 0

    cursor = today_local - timedelta(days=1)
    while cursor >= min_date:
        ci = by_date.get(cursor)
        if ci is None:
            # Intentionally disabled / ungenerated day neither earns nor breaks a streak
            cursor -= timedelta(days=1)
            continue
        if ci.responded_at is not None:
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break
    return streak


class CheckInService:
    """Daily wellbeing check-in, correction, Not well toggling, and urgent help controls."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        clock: Clock,
        notification_service: object | None = None,
    ) -> None:
        self._circle = circle_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._clock = clock
        self._notif = notification_service

    def ensure_today_instance(self, circle_id: uuid.UUID) -> CheckInInstance | None:
        """Create or retrieve today's local-date check-in instance for an enabled circle."""
        circle = self._circle.get_circle(circle_id)
        if not circle or not circle.checkin_enabled:
            return None
        now = self._clock.now()
        tz = ZoneInfo(circle.timezone)
        today_local = now.astimezone(tz).date()
        due_utc, _, _ = resolve_local_instant(today_local, circle.checkin_time, tz)
        window_ends = due_utc + timedelta(minutes=circle.checkin_window_minutes)
        next_due_utc, _, _ = resolve_local_instant(
            today_local + timedelta(days=1), circle.checkin_time, tz
        )
        return self._collab.get_or_create_checkin(
            circle_id=circle_id,
            local_date=today_local,
            due_at=due_utc,
            window_ends_at=window_ends,
            expires_at=next_due_utc,
            now=now,
        )

    def get_today_checkin(self, actor: ActorContext) -> CheckInInstance | None:
        """Return today's check-in instance after enforcing check-in consent."""
        authorize(actor, Action.VIEW_CHECKINS, target_circle_id=actor.circle_id)
        return self.ensure_today_instance(actor.circle_id)

    def list_checkins(
        self, actor: ActorContext, *, days: int = 30
    ) -> tuple[list[CheckInInstance], int]:
        """Return recent check-ins and streak after enforcing CHECKINS consent."""
        authorize(actor, Action.VIEW_CHECKINS, target_circle_id=actor.circle_id)
        circle = self._circle.get_circle(actor.circle_id)
        now = self._clock.now()
        tz = ZoneInfo(actor.circle_timezone)
        today_local = now.astimezone(tz).date()
        start = today_local - timedelta(days=days - 1)
        items = self._collab.list_checkins(actor.circle_id, start, today_local)
        today_ci = next((c for c in items if c.local_date == today_local), None)
        window_closed = bool(today_ci and now >= today_ci.window_ends_at)
        enabled_from = (
            circle.checkin_enabled_from.astimezone(tz).date()
            if circle and circle.checkin_enabled_from
            else None
        )
        streak = compute_checkin_streak(
            items,
            enabled_from_local=enabled_from,
            today_local=today_local,
            current_day_window_closed=window_closed,
        )
        return items, streak

    def submit_checkin(
        self, actor: ActorContext, *, mood: CheckInMood, note: str = ""
    ) -> tuple[CheckInInstance, str]:
        """Submit or correct today's check-in and manage Not well logical event safely."""
        authorize(actor, Action.SUBMIT_CHECKIN, target_circle_id=actor.circle_id)
        ci = self.ensure_today_instance(actor.circle_id)
        if not ci:
            raise ValidationError("errors.validation")
        now = self._clock.now()
        updated, _ = self._collab.submit_checkin_response(
            circle_id=actor.circle_id,
            checkin_id=ci.id,
            mood=mood,
            note=note.strip()[:300],
            actor_account_id=actor.account_id,
            now=now,
        )
        delivery_status = "recorded"
        if self._notif is not None:
            circle = self._circle.get_circle(actor.circle_id)
            if circle:
                if mood == CheckInMood.NOT_WELL:
                    delivery_status = self._notif.queue_and_dispatch_not_well(
                        circle=circle, checkin=updated
                    )
                else:
                    self._notif.cancel_unsent_not_well(checkin_id=updated.id, now=now)
        return updated, delivery_status

    def trigger_urgent_help(self, actor: ActorContext) -> str:
        """Create a separately identified urgent-help event and dispatch immediately."""
        authorize(actor, Action.USE_URGENT_HELP, target_circle_id=actor.circle_id)
        circle = self._circle.get_circle(actor.circle_id)
        if not circle:
            raise ValidationError("errors.not_found")
        now = self._clock.now()
        event_id = uuid.uuid4()
        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=actor.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="urgent_help_triggered",
                target_type="urgent_help",
                target_id=str(event_id),
                occurred_at=now,
            )
        )
        if self._notif is not None:
            return self._notif.queue_and_dispatch_urgent_help(
                circle=circle, event_id=event_id, triggered_at=now
            )
        return "queued"
