"""Medications & Schedules management, refill forecasts, and AI-assisted draft workflows."""

from __future__ import annotations

from datetime import date, time, timedelta

import streamlit as st
from core.errors import CareCircleError
from core.models import Criticality, DoseState
from core.permissions import Action, authorize
from ui.bootstrap import bootstrap_page
from ui.i18n_helpers import format_d, format_tm, render_error, tr

st.set_page_config(page_title="CareCircle — Medications", page_icon="💊", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"💊 {tr('medications.heading')}")

try:
    meds_with_forecast = container.medication.list_medications(actor)
except CareCircleError as exc:
    render_error(exc)
    st.stop()

if not meds_with_forecast:
    st.info(tr("common.empty"))
else:
    for med, sched, forecast in meds_with_forecast:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.subheader(f"{med.name} — {med.dose_text}")
                st.write(f"**{tr(f'criticality.{med.criticality.value}')}** | {med.instructions}")
                if sched:
                    times_str = ", ".join(t_val.strftime("%H:%M") for t_val in sched.times_local)
                    st.caption(f"🕒 {times_str} ({sched.timezone})")
                if forecast.available and forecast.estimated_runout_date is not None:
                    st.write(
                        tr(
                            "medications.refill_forecast",
                            days=forecast.days_remaining,
                            date=format_d(forecast.estimated_runout_date, actor.circle_timezone),
                        )
                    )
                else:
                    st.caption(tr("medications.refill_unavailable"))
                if (
                    med.stock_count is not None
                    and med.refill_threshold is not None
                    and med.stock_count <= med.refill_threshold
                ):
                    st.warning(
                        f"⚠ {tr('medications.low_stock_badge', count=med.stock_count, unit=med.stock_unit)}"
                    )
            with c2:
                try:
                    authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
                    can_edit = True
                except CareCircleError:
                    can_edit = False
                if can_edit:
                    new_stock = st.number_input(
                        tr("medications.stock_count_label"),
                        min_value=0.0,
                        value=float(med.stock_count or 0.0),
                        step=1.0,
                        key=f"stock_{med.id}",
                    )
                    if st.button(tr("common.save"), key=f"save_stock_{med.id}"):
                        container.medication.adjust_stock(
                            actor, medication_id=med.id, new_stock_count=new_stock
                        )
                        st.rerun()
                    if st.button(tr("common.archive"), key=f"arch_{med.id}"):
                        container.medication.archive_medication(actor, medication_id=med.id)
                        st.success(tr("medications.archived"))
                        st.rerun()

# Explicit skip for upcoming doses
try:
    authorize(actor, Action.LOG_DOSE, target_circle_id=actor.circle_id)
    now = container.clock.now()
    doses = container.dose.list_doses_in_window(
        actor, start_utc=now, end_utc=now + timedelta(hours=24)
    )
    pending = [d for d in doses if d.state in (DoseState.PENDING, DoseState.SNOOZED)]
    if pending:
        with st.expander(tr("medications.skip_button")):
            selected_dose = st.selectbox(
                tr("elder_mode.todays_medicines_heading"),
                options=pending,
                format_func=lambda d: (
                    f"{d.medication_name} @ {format_tm(d.due_at, actor.circle_timezone)}"
                ),
            )
            skip_reason = st.text_input(tr("medications.skip_reason_label"))
            if st.button(tr("medications.skip_button"), key="confirm_skip"):
                try:
                    container.dose.skip_dose(actor, dose_id=selected_dose.id, reason=skip_reason)
                    st.rerun()
                except CareCircleError as exc:
                    render_error(exc)
except CareCircleError:
    pass

# Add medication form (for permitted actors)
try:
    authorize(actor, Action.EDIT_MEDICATIONS, target_circle_id=actor.circle_id)
    st.divider()
    st.subheader(tr("medications.add_heading"))
    with st.form("add_med_form"):
        name = st.text_input(tr("medications.name_label"))
        dose_text = st.text_input(tr("medications.dose_text_label"))
        instructions = st.text_input(tr("medications.instructions_label"))
        crit = st.selectbox(
            tr("medications.criticality_label"),
            options=list(Criticality),
            index=1,
            format_func=lambda c: tr(f"criticality.{c.value}"),
        )
        c1, c2 = st.columns(2)
        with c1:
            start_d = st.date_input(tr("medications.start_date_label"), value=date.today())
            has_end = st.checkbox(tr("medications.has_end_date"))
            end_d = st.date_input(tr("medications.end_date_label"), value=date.today())
        with c2:
            times_raw = st.text_input(tr("medications.times_label"), value="08:00, 20:00")
            weekdays = st.multiselect(
                tr("medications.weekdays_label"),
                options=[0, 1, 2, 3, 4, 5, 6],
                default=[0, 1, 2, 3, 4, 5, 6],
                format_func=lambda w: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][w],
            )
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            stock_val = st.number_input(
                tr("medications.stock_count_label"), min_value=0.0, value=30.0
            )
        with s2:
            stock_unit = st.text_input(tr("medications.stock_unit_label"), value="tablets")
        with s3:
            thresh_val = st.number_input(
                tr("medications.refill_threshold_label"), min_value=0.0, value=5.0
            )
        with s4:
            upd_val = st.number_input(
                tr("medications.units_per_dose_label"), min_value=0.0, value=1.0
            )
        sub = st.form_submit_button(tr("medications.add_button"), type="primary")
    if sub:
        try:
            parsed_times = [
                time.fromisoformat(part.strip()) for part in times_raw.split(",") if part.strip()
            ]
            container.medication.create_medication(
                actor,
                name=name,
                dose_text=dose_text,
                instructions=instructions,
                start_date=start_d,
                end_date=end_d if has_end else None,
                criticality=crit,
                stock_count=stock_val if stock_val > 0 else None,
                stock_unit=stock_unit,
                refill_threshold=thresh_val if thresh_val > 0 else None,
                stock_units_per_dose=upd_val if upd_val > 0 else None,
                times_local=parsed_times,
                weekdays=weekdays,
            )
            st.success(tr("medications.saved"))
            st.rerun()
        except (CareCircleError, ValueError) as exc:
            render_error(
                exc if isinstance(exc, CareCircleError) else CareCircleError("errors.validation")
            )

    # Optional Bounded AI Assists (Consent-checked; draft review required)
    st.divider()
    with st.expander(f"✨ {tr('ai.scan_heading')} / {tr('ai.nl_heading')}"):
        st.caption(tr("ai.review_confirm_notice"))
        if not actor.ai_consent_granted or not container.gemini_gateway.enabled:
            st.info(tr("ai.disabled_notice"))
        else:
            nl_txt = st.text_input(tr("ai.nl_heading"))
            if st.button(tr("common.confirm"), key="ai_nl_btn") and nl_txt:
                try:
                    draft = container.ai.parse_nl_schedule(actor, text_input=nl_txt)
                    st.json(draft.model_dump())
                except CareCircleError as exc:
                    render_error(exc)
            uploaded = st.file_uploader(tr("ai.scan_heading"), type=["png", "jpg", "jpeg", "webp"])
            if uploaded is not None and st.button(tr("ai.scan_heading"), key="ai_scan_btn"):
                try:
                    scan = container.ai.scan_prescription(actor, image_bytes=uploaded.read())
                    st.json(scan.model_dump())
                except CareCircleError as exc:
                    render_error(exc)
            voice_file = st.audio_input(tr("ai.voice_heading"))
            if voice_file is not None and st.button(tr("ai.voice_heading"), key="ai_voice_btn"):
                try:
                    vt = container.ai.transcribe_voice(actor, audio_bytes=voice_file.read())
                    st.text_input(tr("ai.voice_transcript_label"), value=vt.transcript)
                except CareCircleError as exc:
                    render_error(exc)
except CareCircleError:
    pass
