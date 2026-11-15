"""Adherence analytics, weekly heatmap + accessible table, dose corrections, and Ask CareCircle."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st
from core.errors import CareCircleError
from core.models import DoseState
from core.permissions import Action, authorize
from services.adherence_service import get_authorized_adherence
from ui.bootstrap import bootstrap_page
from ui.components import render_accessible_table, render_banner
from ui.i18n_helpers import format_dt, render_error, tr

st.set_page_config(page_title="CareCircle — Adherence", page_icon="📊", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"📊 {tr('adherence.heading')}")
render_banner("ℹ️", tr("adherence.disclaimer"))

try:
    authorize(actor, Action.VIEW_ADHERENCE, target_circle_id=actor.circle_id)
    window = st.radio(
        tr("adherence.heading"),
        options=[7, 30],
        horizontal=True,
        format_func=lambda d: tr("adherence.window_7d") if d == 7 else tr("adherence.window_30d"),
    )
    now = container.clock.now()
    doses = container.repos.dose.list_doses_in_window(
        actor.circle_id, now - timedelta(days=35), now + timedelta(hours=1)
    )
    summary = get_authorized_adherence(
        actor,
        doses=doses,
        window_days=window,
        grace_minutes=container.config.adherence_grace_minutes,
        now=now,
    )
except CareCircleError as exc:
    render_error(exc)
    st.stop()

if summary.total_past_due == 0:
    st.info(tr("adherence.zero_denominator"))
else:
    c1, c2 = st.columns(2)
    with c1:
        st.metric(
            label=tr("adherence.window_7d") if window == 7 else tr("adherence.window_30d"),
            value=f"{summary.rate_percent}%",
        )
        st.write(
            tr(
                "adherence.overall_rate",
                rate=summary.rate_percent,
                taken=summary.taken,
                total=summary.total_past_due,
            )
        )
        st.write(
            tr(
                "adherence.on_time_late",
                grace=container.config.adherence_grace_minutes,
                on_time=summary.on_time,
                late=summary.late,
            )
        )
        st.write(
            tr(
                "adherence.other_counts",
                cant_take=summary.cant_take,
                skipped=summary.skipped,
                unconfirmed=summary.unconfirmed,
            )
        )
    with c2:
        st.metric(
            label=tr("adherence.streak_label", days=summary.streak_days),
            value=f"{summary.streak_days}",
        )

    st.subheader(tr("adherence.table_heading"))
    med_rows = [
        [
            m.medication_name,
            f"{m.taken}/{m.total_past_due}",
            f"{m.rate_percent}%" if m.rate_percent is not None else tr("common.unavailable"),
            m.on_time,
            m.late,
            m.cant_take,
            m.skipped,
            m.unconfirmed,
        ]
        for m in summary.by_medication
    ]
    render_accessible_table(
        headers=[
            "Medication",
            "Taken/Due",
            "Rate",
            "On-time",
            "Late",
            "Can't take",
            "Skipped",
            "Unconfirmed",
        ],
        rows=med_rows,
        caption=tr("adherence.heading"),
    )

    st.subheader(tr("adherence.heatmap_heading"))
    day_rows = [
        [
            r["date"],
            r["taken"],
            r["total"],
            f"{r['rate_percent']}%" if r["rate_percent"] is not None else "-",
            r["cant_take"],
            r["skipped"],
            r["unconfirmed"],
        ]
        for r in summary.daily_rows
    ]
    render_accessible_table(
        headers=["Date", "Taken", "Due", "Rate", "Can't take", "Skipped", "Unconfirmed"],
        rows=day_rows,
        caption=tr("adherence.heatmap_heading"),
    )

# Audited Dose Correction flow
try:
    authorize(actor, Action.CORRECT_DOSE, target_circle_id=actor.circle_id)
    past_doses = [d for d in doses if d.due_at <= now and d.state != DoseState.CANCELLED]
    if past_doses:
        st.divider()
        with st.expander(tr("medications.correct_heading")):
            target = st.selectbox(
                tr("elder_mode.todays_medicines_heading"),
                options=list(reversed(past_doses))[:30],
                format_func=lambda d: (
                    f"{d.medication_name} ({format_dt(d.due_at, actor.circle_timezone)}) — {d.state.value}"
                ),
            )
            new_st = st.selectbox(
                tr("common.status_label"),
                options=[DoseState.TAKEN, DoseState.SKIPPED, DoseState.EXPIRED],
                format_func=lambda s: tr(f"dose_states.{s.value}"),
            )
            corr_reason = st.text_input(tr("medications.correct_reason_label"))
            if st.button(tr("medications.correct_button")):
                try:
                    container.dose.correct_dose(
                        actor, dose_id=target.id, new_state=new_st, reason=corr_reason
                    )
                    st.rerun()
                except CareCircleError as exc:
                    render_error(exc)
except CareCircleError:
    pass

# Ask CareCircle (Administrative Q&A)
st.divider()
with st.expander(f"💬 {tr('ai.ask_heading')}"):
    st.caption(tr("ai.disclaimer"))
    q = st.text_input(tr("ai.ask_heading"), key="ask_cc_input")
    if st.button(tr("common.confirm"), key="ask_cc_btn") and q:
        try:
            ans = container.ai.ask_carecircle(actor, question=q)
            st.write(ans.answer)
            if ans.cited_record_ids:
                st.caption(", ".join(ans.cited_record_ids))
        except CareCircleError as exc:
            render_error(exc)
