"""Background reminder worker orchestration with bounded execution budget and health metrics."""

from __future__ import annotations

import time as monotonic_time
import uuid
from datetime import timedelta

from core.clock import Clock
from core.config import AppConfig
from core.logging import get_logger
from core.models import CheckInMood
from core.models_clinical import WorkerHealth
from repositories.interfaces import (
    CircleRepository,
    CollabRepository,
    DoseRepository,
    OutboxRepository,
)

from services.appointment_service import select_appointment_reminder_level
from services.checkin_service import CheckInPolicy, CheckInService, next_checkin_action
from services.escalation_service import (
    DoseStatusSnapshot,
    EscalationProgress,
    Policy,
    next_action,
)
from services.notification_service import NotificationService
from services.schedule_service import ScheduleService

_LOG = get_logger("carecircle.worker")


class WorkerService:
    """Executes bounded reminder cycles for schedules, Care Chain, check-ins, appointments, and outbox."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        dose_repo: DoseRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        schedule_service: ScheduleService,
        checkin_service: CheckInService,
        notification_service: NotificationService,
        config: AppConfig,
        clock: Clock,
    ) -> None:
        self._circle = circle_repo
        self._dose = dose_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._sched = schedule_service
        self._checkin = checkin_service
        self._notif = notification_service
        self._cfg = config
        self._clock = clock

    def run_cycle(self) -> dict[str, int]:
        """Run one idempotent bounded-budget reminder worker cycle."""
        started = monotonic_time.monotonic()
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        now = self._clock.now()
        summary: dict[str, int] = {
            "doses_generated": 0,
            "doses_expired": 0,
            "dose_escalations_queued": 0,
            "checkin_escalations_queued": 0,
            "appointment_reminders_queued": 0,
            "outbox_sent": 0,
            "outbox_cancelled": 0,
            "outbox_uncertain": 0,
        }

        try:
            summary["doses_generated"] = self._sched.reconcile_all_circles(max_days_chunk=7)
            summary["doses_expired"] = self._dose.expire_overdue_doses(now=now)

            for circle in self._circle.list_active_circles():
                if monotonic_time.monotonic() - started > self._cfg.worker_budget_seconds:
                    break
                if circle.checkin_enabled:
                    self._checkin.ensure_today_instance(circle.id)

            active_doses = self._dose.list_active_pending_doses(
                now, limit=self._cfg.worker_batch_size
            )
            circles_cache = {c.id: c for c in self._circle.list_active_circles()}
            for dose in active_doses:
                if monotonic_time.monotonic() - started > self._cfg.worker_budget_seconds:
                    break
                circle = circles_cache.get(dose.circle_id)
                if not circle:
                    continue
                ack = self._collab.get_acknowledgement(episode_type="dose", resource_id=dose.id)
                progress = EscalationProgress.from_dict(
                    dose.escalation_high_water, dose.escalation_state
                )
                policy = Policy(
                    criticality=dose.criticality,
                    timezone=circle.timezone,
                    quiet_hours_enabled=circle.quiet_hours_enabled,
                    quiet_hours_start=circle.quiet_hours_start,
                    quiet_hours_end=circle.quiet_hours_end,
                    elder_quiet_reminders_enabled=circle.elder_quiet_reminders_enabled,
                    expires_at=dose.expires_at,
                )
                status = DoseStatusSnapshot(
                    state=dose.state,
                    snoozed_until=dose.snoozed_until,
                    cant_take_report_id=(
                        str(dose.cant_take_report_id) if dose.cant_take_report_id else None
                    ),
                    cant_take_reason=dose.cant_take_reason,
                )
                act = next_action(
                    dose.due_at,
                    now,
                    status,
                    progress,
                    policy,
                    acknowledged=(ack is not None),
                )
                if act.kind != "none" and act.next_progress is not None:
                    self._dose.update_escalation_progress(
                        dose_id=dose.id,
                        high_water=act.next_progress.high_water_level,
                        escalation_state=act.next_progress.to_dict(),
                        now=now,
                    )
                    if act.kind != "explicit_help_report":
                        items = self._notif.queue_dose_escalation(
                            circle=circle,
                            dose=dose,
                            level=act.level,
                            target_role=act.target_role,
                        )
                        summary["dose_escalations_queued"] += len(items)

            for ci in self._collab.list_active_unresponded_checkins(now):
                if monotonic_time.monotonic() - started > self._cfg.worker_budget_seconds:
                    break
                circle = circles_cache.get(ci.circle_id)
                if not circle:
                    continue
                ack = self._collab.get_acknowledgement(episode_type="checkin", resource_id=ci.id)
                progress = EscalationProgress.from_dict(
                    ci.escalation_high_water, ci.escalation_state
                )
                c_policy = CheckInPolicy(
                    timezone=circle.timezone,
                    window_minutes=circle.checkin_window_minutes,
                    quiet_hours_enabled=circle.quiet_hours_enabled,
                    quiet_hours_start=circle.quiet_hours_start,
                    quiet_hours_end=circle.quiet_hours_end,
                    elder_quiet_reminders_enabled=circle.elder_quiet_reminders_enabled,
                )
                act = next_checkin_action(
                    due_at=ci.due_at,
                    window_ends_at=ci.window_ends_at,
                    expires_at=ci.expires_at,
                    now=now,
                    responded=(ci.responded_at is not None),
                    mood=ci.mood,
                    sent_levels=progress,
                    policy=c_policy,
                    acknowledged=(ack is not None),
                    not_well_report_id=str(ci.id) if ci.mood == CheckInMood.NOT_WELL else "",
                )
                if act.kind != "none" and act.next_progress is not None:
                    self._collab.update_checkin_escalation(
                        checkin_id=ci.id,
                        high_water=act.next_progress.high_water_level,
                        escalation_state=act.next_progress.to_dict(),
                        now=now,
                    )
                    items = self._notif.queue_checkin_escalation(
                        circle=circle,
                        checkin=ci,
                        level=act.level,
                        target_role=act.target_role,
                    )
                    summary["checkin_escalations_queued"] += len(items)

            for appt in self._collab.list_upcoming_appointments(now, now + timedelta(hours=24)):
                circle = circles_cache.get(appt.circle_id)
                if not circle:
                    continue
                lvl = select_appointment_reminder_level(starts_at=appt.starts_at, now=now)
                if lvl is not None:
                    items = self._notif.queue_appointment_reminder(
                        circle=circle, appointment=appt, level=lvl
                    )
                    summary["appointment_reminders_queued"] += len(items)

            dispatch_stats = self._notif.process_due_outbox(
                worker_id=worker_id, batch_size=self._cfg.worker_batch_size
            )
            summary["outbox_sent"] = dispatch_stats["sent"]
            summary["outbox_cancelled"] = dispatch_stats["cancelled"]
            summary["outbox_uncertain"] = dispatch_stats["uncertain"]

            metrics = self._outbox.count_outbox_metrics(now)
            self._outbox.save_worker_health(
                WorkerHealth(
                    worker_name="reminders",
                    last_run_at=now,
                    last_success_at=now,
                    last_error_category="",
                    due_event_lag_seconds=metrics["due_lag_seconds"],
                    uncertain_send_count=metrics["uncertain_count"],
                    last_summary=summary,
                )
            )
            _LOG.info("worker_cycle_completed", **summary)
            return summary
        except Exception as exc:
            prev = self._outbox.get_worker_health("reminders")
            self._outbox.save_worker_health(
                WorkerHealth(
                    worker_name="reminders",
                    last_run_at=now,
                    last_success_at=prev.last_success_at,
                    last_error_category=type(exc).__name__,
                    due_event_lag_seconds=prev.due_event_lag_seconds,
                    uncertain_send_count=prev.uncertain_send_count,
                    last_summary=summary,
                )
            )
            _LOG.error("worker_cycle_failed", error_type=type(exc).__name__)
            raise
