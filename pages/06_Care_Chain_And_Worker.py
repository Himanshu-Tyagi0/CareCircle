"""Care acknowledgements, notification outbox visibility, worker health, and isolated Care Chain simulator."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import streamlit as st
from core.errors import CareCircleError
from core.models import CantTakeReason, Criticality, DoseState, OutboxStatus, Role
from core.permissions import Action, authorize
from services.escalation_service import (
    DoseStatusSnapshot,
    EscalationProgress,
    Policy,
    next_action,
)
from ui.bootstrap import bootstrap_page
from ui.components import render_accessible_table, render_banner
from ui.i18n_helpers import format_dt, tr

st.set_page_config(page_title="CareCircle — Care Chain", page_icon="🤝", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"🤝 {tr('care_chain.heading')}")

# 1. Active doses needing acknowledgement
now = container.clock.now()
doses = container.repos.dose.list_doses_in_window(
    actor.circle_id, now - timedelta(hours=12), now + timedelta(hours=6)
)
circle = container.circle.get_circle(actor)

try:
    authorize(actor, Action.ACKNOWLEDGE_CARE, target_circle_id=actor.circle_id)
    for d in doses:
        if (
            d.state in (DoseState.PENDING, DoseState.SNOOZED, DoseState.CANT_TAKE)
            and d.due_at <= now
        ):
            ack = container.repos.collab.get_acknowledgement(episode_type="dose", resource_id=d.id)
            with st.container(border=True):
                st.write(
                    f"**{d.medication_name}** ({d.dose_text}) — {format_dt(d.due_at, circle.timezone)}"
                )
                if ack:
                    st.info(
                        tr(
                            "care_chain.ack_recorded",
                            name=ack.acknowledged_by_name,
                            elder=circle.display_name,
                        )
                    )
                elif st.button(tr("care_chain.ack_button"), key=f"ack_btn_{d.id}", type="primary"):
                    ack_rec, _ = container.notifications.acknowledge_episode(
                        actor, episode_type="dose", resource_id=d.id
                    )
                    st.success(
                        tr(
                            "care_chain.ack_recorded",
                            name=ack_rec.acknowledged_by_name,
                            elder=circle.display_name,
                        )
                    )
                    st.rerun()
except CareCircleError:
    pass

# 2. Outbox Visibility & Reconciliation (Elder)
st.divider()
st.subheader(tr("care_chain.outbox_heading"))
outbox_items = container.repos.outbox.list_outbox(actor.circle_id, limit=30)
if not outbox_items:
    st.info(tr("common.empty"))
else:
    rows = [
        [
            format_dt(o.created_at, circle.timezone),
            o.event_type,
            o.channel.value,
            o.status.value,
            o.attempts,
            o.last_error_code or "-",
        ]
        for o in outbox_items
    ]
    render_accessible_table(
        headers=["Time", "Event", "Channel", "Status", "Attempts", "Error"],
        rows=rows,
        caption=tr("care_chain.outbox_heading"),
    )
    if actor.role == Role.ELDER:
        for o in outbox_items:
            if o.status == OutboxStatus.UNCERTAIN:
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    st.warning(f"{o.event_type} ({o.channel.value}) — UNCERTAIN")
                with c2:
                    if st.button(tr("care_chain.reconcile_uncertain_sent"), key=f"rec_sent_{o.id}"):
                        container.notifications.reconcile_uncertain(
                            actor, item_id=o.id, resolution="sent"
                        )
                        st.rerun()
                with c3:
                    if st.button(tr("care_chain.reconcile_uncertain_retry"), key=f"rec_ret_{o.id}"):
                        container.notifications.reconcile_uncertain(
                            actor, item_id=o.id, resolution="retry"
                        )
                        st.rerun()

# 3. Worker Health & Demo Run
st.divider()
st.subheader(tr("care_chain.worker_heading"))
health = container.repos.outbox.get_worker_health("reminders")
st.json(
    {
        "last_run_at": health.last_run_at.isoformat() if health.last_run_at else None,
        "last_success_at": health.last_success_at.isoformat() if health.last_success_at else None,
        "due_event_lag_seconds": health.due_event_lag_seconds,
        "uncertain_send_count": health.uncertain_send_count,
        "last_summary": health.last_summary,
    }
)
if container.config.demo_mode and st.button(tr("care_chain.run_worker_now")):
    summary = container.worker.run_cycle()
    st.success(str(summary))

# 4. Isolated Care Chain Policy Simulator (never alters production state or sends real messages)
st.divider()
st.subheader(tr("care_chain.simulator_heading"))
render_banner("🧪", tr("care_chain.simulator_notice"))
sim_crit = st.selectbox(
    tr("medications.criticality_label"),
    options=list(Criticality),
    format_func=lambda c: tr(f"criticality.{c.value}"),
)
sim_delay = st.slider("Minutes elapsed after due time", min_value=0, max_value=120, value=35)
sim_quiet = st.checkbox("Simulate Quiet Hours active", value=False)
sim_ack = st.checkbox("Simulate Contact Acknowledged", value=False)
sim_cant_take = st.checkbox("Simulate Can't Take reported", value=False)

base_due = datetime(2026, 1, 15, 3, 30, tzinfo=UTC)  # 09:00 IST
sim_now = base_due + timedelta(minutes=sim_delay)
sim_policy = Policy(
    criticality=sim_crit,
    timezone="UTC",
    quiet_hours_enabled=sim_quiet,
    quiet_hours_start=time(0, 0),
    quiet_hours_end=time(23, 59),
)
sim_status = DoseStatusSnapshot(
    state=DoseState.CANT_TAKE if sim_cant_take else DoseState.PENDING,
    cant_take_report_id="sim-rep-1" if sim_cant_take else None,
    cant_take_reason=CantTakeReason.UNWELL if sim_cant_take else None,
)
sim_action = next_action(base_due, sim_now, sim_status, EscalationProgress(), sim_policy, sim_ack)
st.write(
    f"**Action:** `{sim_action.kind}` | **Level:** `{sim_action.level}` | **Target:** `{sim_action.target_role}`"
)
