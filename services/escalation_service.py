"""Pure Care Chain escalation policy for unconfirmed doses and explicit help reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from core.models import CantTakeReason, Criticality, DoseState


class LevelDisposition(StrEnum):
    """Persisted disposition of a Care Chain escalation level."""

    QUEUED = "queued"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class DoseStatusSnapshot:
    """Typed immutable dose status passed into pure next_action policy."""

    state: DoseState
    snoozed_until: datetime | None = None
    cant_take_report_id: str | None = None
    cant_take_reason: CantTakeReason | None = None


@dataclass(frozen=True)
class EscalationProgress:
    """Monotonic Care Chain progress replacing a bare set of sent levels."""

    high_water_level: int = 0
    dispositions: Mapping[int, LevelDisposition] = field(default_factory=dict)
    handled_report_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, high_water: int, data: dict[str, object] | None) -> EscalationProgress:
        """Reconstruct immutable progress from persisted JSON state."""
        raw = data or {}
        raw_disp = raw.get("dispositions", {})
        disp: dict[int, LevelDisposition] = {}
        if isinstance(raw_disp, dict):
            for k, v in raw_disp.items():
                disp[int(k)] = LevelDisposition(str(v))
        raw_reports = raw.get("handled_report_ids", [])
        reports = (
            frozenset(str(r) for r in raw_reports) if isinstance(raw_reports, list) else frozenset()
        )
        return cls(high_water_level=high_water, dispositions=disp, handled_report_ids=reports)

    def to_dict(self) -> dict[str, object]:
        """Serialize progress safely for persistence."""
        return {
            "dispositions": {str(k): v.value for k, v in sorted(self.dispositions.items())},
            "handled_report_ids": sorted(self.handled_report_ids),
        }


@dataclass(frozen=True)
class Policy:
    """Typed immutable Care Chain escalation policy."""

    criticality: Criticality = Criticality.NORMAL
    timezone: str = "Asia/Kolkata"
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)
    elder_quiet_reminders_enabled: bool = True
    expiry_hours: int = 12
    expires_at: datetime | None = None
    windows_override_minutes: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Action:
    """Pure output describing what Care Chain action should occur, if any."""

    kind: str  # "none", "elder_reminder", "elder_again", "primary_contact", "secondary_contact", "explicit_help_report"
    level: int = 0
    target_role: str = ""  # "elder", "primary_contact", "secondary_contact"
    reason: str = ""
    report_id: str = ""
    next_progress: EscalationProgress | None = None


_DEFAULT_WINDOWS: dict[Criticality, tuple[tuple[int, int, str, str], ...]] = {
    Criticality.NORMAL: (
        (1, 0, "elder_reminder", "elder"),
        (2, 15, "elder_again", "elder"),
        (3, 30, "primary_contact", "primary_contact"),
        (4, 60, "secondary_contact", "secondary_contact"),
    ),
    Criticality.CRITICAL: (
        (1, 0, "elder_reminder", "elder"),
        (2, 10, "elder_again", "elder"),
        (3, 20, "primary_contact", "primary_contact"),
        (4, 40, "secondary_contact", "secondary_contact"),
    ),
    Criticality.OPTIONAL: ((1, 0, "elder_reminder", "elder"),),
}


def is_in_quiet_hours(now: datetime, policy: Policy) -> bool:
    """Return True if `now` falls inside the policy's quiet hours in the elder's timezone."""
    if not policy.quiet_hours_enabled:
        return False
    local_t = now.astimezone(ZoneInfo(policy.timezone)).time()
    start = policy.quiet_hours_start
    end = policy.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= local_t < end
    return local_t >= start or local_t < end


def next_action(
    due_at: datetime,
    now: datetime,
    status: DoseStatusSnapshot,
    sent_levels: EscalationProgress,
    policy: Policy,
    acknowledged: bool,
) -> Action:
    """Determine the single next Care Chain action purely from timestamps and progress.

    Never reads the wall clock; uses monotonic high-water level tracking so delayed
    cron runs emit only the highest eligible level and mark skipped lower levels superseded.
    """
    effective_expiry = policy.expires_at or (due_at + timedelta(hours=policy.expiry_hours))

    if status.state in (
        DoseState.TAKEN,
        DoseState.SKIPPED,
        DoseState.CANCELLED,
        DoseState.EXPIRED,
    ):
        return Action(kind="none", reason=f"terminal:{status.state.value}")

    if status.state == DoseState.CANT_TAKE:
        rep_id = status.cant_take_report_id or ""
        if rep_id and rep_id not in sent_levels.handled_report_ids:
            new_disp = dict(sent_levels.dispositions)
            new_disp[3] = LevelDisposition.QUEUED
            next_prog = EscalationProgress(
                high_water_level=max(sent_levels.high_water_level, 3),
                dispositions=new_disp,
                handled_report_ids=sent_levels.handled_report_ids | {rep_id},
            )
            return Action(
                kind="explicit_help_report",
                level=3,
                target_role="primary_contact",
                reason=status.cant_take_reason.value if status.cant_take_reason else "",
                report_id=rep_id,
                next_progress=next_prog,
            )
        return Action(kind="none", reason="cant_take_already_handled")

    if now >= effective_expiry:
        return Action(kind="none", reason="expired")

    if acknowledged:
        return Action(kind="none", reason="acknowledged")

    if (
        status.state == DoseState.SNOOZED
        and status.snoozed_until is not None
        and now < status.snoozed_until
    ):
        return Action(kind="none", reason="snoozed")

    if now < due_at:
        return Action(kind="none", reason="not_due_yet")

    elapsed_minutes = (now - due_at).total_seconds() / 60.0
    quiet = is_in_quiet_hours(now, policy)

    schedule_levels = _DEFAULT_WINDOWS[policy.criticality]
    if policy.windows_override_minutes is not None:
        base_roles = [
            (1, "elder_reminder", "elder"),
            (2, "elder_again", "elder"),
            (3, "primary_contact", "primary_contact"),
            (4, "secondary_contact", "secondary_contact"),
        ]
        schedule_levels = tuple(
            (lvl, mins, kind, role)
            for (lvl, kind, role), mins in zip(
                base_roles, policy.windows_override_minutes, strict=False
            )
        )

    eligible: list[tuple[int, str, str]] = []
    for level, offset_min, kind, target_role in schedule_levels:
        if level <= sent_levels.high_water_level:
            continue
        if elapsed_minutes < offset_min:
            continue
        if quiet:
            if target_role == "elder" and not policy.elder_quiet_reminders_enabled:
                continue
            if (
                target_role in ("primary_contact", "secondary_contact")
                and policy.criticality != Criticality.CRITICAL
            ):
                continue
        eligible.append((level, kind, target_role))

    if not eligible:
        return Action(kind="none", reason="waiting_or_quiet")

    chosen_level, chosen_kind, chosen_role = eligible[-1]
    new_disp = dict(sent_levels.dispositions)
    for lvl, _, _, _ in schedule_levels:
        if sent_levels.high_water_level < lvl < chosen_level and lvl not in new_disp:
            new_disp[lvl] = LevelDisposition.SUPERSEDED
    new_disp[chosen_level] = LevelDisposition.QUEUED

    next_prog = EscalationProgress(
        high_water_level=chosen_level,
        dispositions=new_disp,
        handled_report_ids=sent_levels.handled_report_ids,
    )
    return Action(
        kind=chosen_kind,
        level=chosen_level,
        target_role=chosen_role,
        next_progress=next_prog,
    )
