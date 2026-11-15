"""Calm Elder Mode home screen: today's medicines, check-in, acknowledgement, read-aloud, and urgent help."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from core.errors import CareCircleError
from core.models import CantTakeReason, CheckInMood, ConsentCategory, DoseState, Role
from services.circle_service import EMERGENCY_NUMBERS_BY_COUNTRY
from ui.bootstrap import bootstrap_page
from ui.components import render_banner, render_read_aloud_controls
from ui.i18n_helpers import current_lang, format_tm, render_error, tr

st.set_page_config(page_title="CareCircle — Elder Home", page_icon="🩺", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

circle = container.circle.get_circle(actor)
tz = ZoneInfo(circle.timezone)
now = container.clock.now()
today_local = now.astimezone(tz).date()
start_day_utc = datetime.combine(today_local, time.min, tzinfo=tz).astimezone(ZoneInfo("UTC"))
end_day_utc = start_day_utc + timedelta(days=1)

st.title(f"🏡 {tr('elder_mode.heading')} — {tr('elder_mode.greeting', name=actor.display_name)}")

# Top quick controls (language & text size inside Elder Mode too)
q_col1, q_col2 = st.columns([2, 1])
with q_col1:
    st.caption(tr("common.timezone_label", tz=circle.timezone))
with q_col2:
    if actor.role == Role.ELDER and st.button(
        f"🆘 {tr('elder_mode.urgent_help_button')}", type="primary", use_container_width=True
    ):
        st.session_state["confirm_urgent_help"] = True

if st.session_state.get("confirm_urgent_help"):
    with st.container(border=True):
        st.subheader(f"🆘 {tr('elder_mode.urgent_help_confirm_heading')}")
        st.warning(tr("elder_mode.urgent_help_confirm_body"))
        c1, c2 = st.columns(2)
        with c1:
            if st.button(tr("common.confirm"), key="urgent_confirm", type="primary"):
                try:
                    container.checkin.trigger_urgent_help(actor)
                    st.session_state.pop("confirm_urgent_help", None)
                    st.success(tr("elder_mode.urgent_help_sent"))
                except CareCircleError as exc:
                    render_error(exc)
        with c2:
            if st.button(tr("common.cancel"), key="urgent_cancel"):
                st.session_state.pop("confirm_urgent_help", None)
                st.rerun()

# Always-available non-diagnostic emergency panel
em_num = EMERGENCY_NUMBERS_BY_COUNTRY.get(circle.emergency_country_code or "")
if em_num:
    render_banner("📞", tr("app.emergency_notice", emergency_number=em_num), role="alert")
else:
    render_banner("📞", tr("app.emergency_notice_no_region"), role="alert")
if circle.emergency_contact_name or circle.emergency_contact_phone:
    st.caption(
        f"**{tr('elder_mode.emergency_contacts_heading')}:** "
        f"{circle.emergency_contact_name or ''} ({circle.emergency_contact_phone or ''})"
    )

# Care Acknowledgement status
acks = container.repos.collab.list_acknowledgements(actor.circle_id, limit=1)
if acks:
    latest_ack = acks[0]
    render_banner(
        "🤝",
        tr(
            "elder_mode.care_ack_active",
            name=latest_ack.acknowledged_by_name,
            time=format_tm(latest_ack.acknowledged_at, circle.timezone),
        ),
    )

# Today's medicines (enforced via DoseService + Consent)
read_parts: list[str] = []
if ConsentCategory.MEDICATIONS in actor.consents:
    doses = container.dose.list_doses_in_window(actor, start_utc=start_day_utc, end_utc=end_day_utc)
    doses = [d for d in doses if d.state != DoseState.CANCELLED]
    pending = [d for d in doses if d.state in (DoseState.PENDING, DoseState.SNOOZED)]
    next_due = pending[0] if pending else None

    st.subheader(f"💊 {tr('elder_mode.next_due_heading')}")
    if next_due is None:
        st.success(f"✅ {tr('elder_mode.all_caught_up')}")
        read_parts.append(tr("elder_mode.all_caught_up"))
    else:
        due_str = format_tm(next_due.due_at, circle.timezone)
        with st.container(border=True):
            st.markdown(f"### {next_due.medication_name} — {next_due.dose_text}")
            st.write(f"🕒 **{due_str}** | {tr(f'criticality.{next_due.criticality.value}')}")
            if next_due.instructions:
                st.write(next_due.instructions)
            read_parts.append(f"{next_due.medication_name}, {next_due.dose_text}, {due_str}.")
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button(
                    f"✅ {tr('elder_mode.taken_button')}",
                    key="next_taken",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        res = container.dose.mark_taken(actor, dose_id=next_due.id)
                        t_txt = format_tm(res.dose.confirmed_at or now, circle.timezone)
                        if res.already_confirmed:
                            st.info(tr("elder_mode.dose_already_confirmed", time=t_txt))
                        else:
                            st.success(tr("elder_mode.dose_confirmed_at", time=t_txt))
                        if res.inventory_discrepancy:
                            st.warning(tr("elder_mode.inventory_discrepancy_warning"))
                    except CareCircleError as exc:
                        render_error(exc)
            with b2:
                if st.button(
                    f"⏱ {tr('elder_mode.snooze_button')}",
                    key="next_snooze",
                    use_container_width=True,
                ):
                    try:
                        res = container.dose.snooze_dose(actor, dose_id=next_due.id, minutes=10)
                        st.info(
                            tr(
                                "elder_mode.dose_snoozed_until",
                                time=format_tm(res.dose.snoozed_until, circle.timezone),
                            )
                        )
                    except CareCircleError as exc:
                        render_error(exc)
            with b3:
                if st.button(
                    f"⚠ {tr('elder_mode.cant_take_button')}",
                    key="next_cant_take",
                    use_container_width=True,
                ):
                    st.session_state["cant_take_target_id"] = str(next_due.id)

    if st.session_state.get("cant_take_target_id"):
        target_id = st.session_state["cant_take_target_id"]
        with st.container(border=True):
            st.subheader(tr("elder_mode.cant_take_heading"))
            rec_name = tr("circle.primary_contact")
            st.info(tr("elder_mode.cant_take_disclose", recipient=rec_name))
            with st.form("cant_take_form"):
                reason = st.selectbox(
                    tr("elder_mode.cant_take_reason_label"),
                    options=list(CantTakeReason),
                    format_func=lambda r: tr(f"cant_take_reasons.{r.value}"),
                )
                note = st.text_input(tr("elder_mode.cant_take_note_label"))
                sub_ct = st.form_submit_button(tr("elder_mode.cant_take_submit"), type="primary")
            if sub_ct:
                try:
                    from uuid import UUID

                    container.dose.report_cant_take(
                        actor, dose_id=UUID(target_id), reason=reason, note=note
                    )
                    st.session_state.pop("cant_take_target_id", None)
                    st.success(tr("elder_mode.dose_cant_take_recorded"))
                except CareCircleError as exc:
                    render_error(exc)

    st.subheader(f"📋 {tr('elder_mode.todays_medicines_heading')}")
    if not doses:
        st.info(tr("elder_mode.no_medicines_today"))
    else:
        for d in doses:
            due_str = format_tm(d.due_at, circle.timezone)
            if d.state == DoseState.SNOOZED and d.snoozed_until:
                st_txt = tr("dose_states.snoozed", time=format_tm(d.snoozed_until, circle.timezone))
            else:
                st_txt = tr(f"dose_states.{d.state.value}")
            icon = (
                "✅"
                if d.state == DoseState.TAKEN
                else "🕒"
                if d.state in (DoseState.PENDING, DoseState.SNOOZED)
                else "⚠"
            )
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.write(f"**{due_str}** — **{d.medication_name}** ({d.dose_text})")
                    if d.instructions:
                        st.caption(d.instructions)
                with c2:
                    st.write(f"{icon} {st_txt}")
            read_parts.append(f"{due_str}: {d.medication_name}, {st_txt}.")

# Daily Wellbeing Check-in card
if ConsentCategory.CHECKINS in actor.consents and circle.checkin_enabled:
    st.subheader(f"💬 {tr('elder_mode.checkin_card_heading')}")
    ci = container.checkin.get_today_checkin(actor)
    if ci and ci.mood:
        st.success(f"✅ {tr('elder_mode.checkin_saved', mood=tr(f'moods.{ci.mood.value}'))}")
    if actor.role == Role.ELDER:
        chk_note = st.text_input(tr("elder_mode.checkin_note_label"), value=ci.note if ci else "")
        st.caption(
            tr("elder_mode.checkin_not_well_disclose", recipient=tr("circle.primary_contact"))
        )
        m1, m2, m3, m4 = st.columns(4)
        for col, label_key, mood_val in (
            (m1, "elder_mode.checkin_im_okay_button", CheckInMood.GOOD),
            (m2, "elder_mode.checkin_good_button", CheckInMood.GOOD),
            (m3, "elder_mode.checkin_okay_button", CheckInMood.OKAY),
            (m4, "elder_mode.checkin_not_well_button", CheckInMood.NOT_WELL),
        ):
            with col:
                if st.button(tr(label_key), key=f"chk_{label_key}", use_container_width=True):
                    try:
                        container.checkin.submit_checkin(actor, mood=mood_val, note=chk_note)
                        st.rerun()
                    except CareCircleError as exc:
                        render_error(exc)

if read_parts:
    st.divider()
    render_read_aloud_controls(" ".join(read_parts), lang=current_lang())
