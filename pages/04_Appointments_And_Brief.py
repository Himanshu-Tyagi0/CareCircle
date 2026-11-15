"""Appointments CRUD, family notes/elder questions, deterministic Doctor Brief, and EN/HI PDF."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import streamlit as st
from core.errors import CareCircleError
from core.models import ConsentCategory, Role
from core.permissions import Action, authorize
from services.pdf_service import render_brief_pdf
from ui.bootstrap import bootstrap_page
from ui.components import render_banner
from ui.i18n_helpers import format_dt, render_error, tr

st.set_page_config(page_title="CareCircle — Appointments & Brief", page_icon="🗓", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"🗓 {tr('appointments.heading')}")

# 1. Appointments section
if ConsentCategory.APPOINTMENTS in actor.consents:
    try:
        appts = container.appointment.list_appointments(actor)
        for a in appts:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.write(f"**{a.doctor_name}** ({a.specialty}) — {a.location}")
                    st.write(f"🕒 {format_dt(a.starts_at, a.timezone)}")
                    if a.notes:
                        st.caption(a.notes)
                with c2:
                    try:
                        authorize(
                            actor, Action.MANAGE_APPOINTMENTS, target_circle_id=actor.circle_id
                        )
                        if st.button(tr("common.cancel"), key=f"cancel_appt_{a.id}"):
                            container.appointment.cancel_appointment(actor, appointment_id=a.id)
                            st.success(tr("appointments.cancelled_msg"))
                            st.rerun()
                    except CareCircleError:
                        pass
    except CareCircleError as exc:
        render_error(exc)

    try:
        authorize(actor, Action.MANAGE_APPOINTMENTS, target_circle_id=actor.circle_id)
        with st.expander(tr("appointments.add_heading")):
            with st.form("add_appt_form"):
                doc = st.text_input(tr("appointments.doctor_label"))
                spec = st.text_input(tr("appointments.specialty_label"))
                loc = st.text_input(tr("appointments.location_label"))
                d_val = st.date_input(tr("appointments.date_label"), value=date.today())
                t_val = st.time_input(tr("appointments.time_label"), value=time(10, 30))
                notes = st.text_input(tr("appointments.notes_label"))
                sub = st.form_submit_button(tr("appointments.add_button"), type="primary")
            if sub:
                try:
                    tz = ZoneInfo(actor.circle_timezone)
                    starts = datetime.combine(d_val, t_val, tzinfo=tz).astimezone(ZoneInfo("UTC"))
                    container.appointment.create_appointment(
                        actor,
                        doctor_name=doc,
                        specialty=spec,
                        location=loc,
                        starts_at=starts,
                        notes=notes,
                    )
                    st.success(tr("appointments.saved"))
                    st.rerun()
                except CareCircleError as exc:
                    render_error(exc)
    except CareCircleError:
        pass
else:
    st.info(tr("errors.consent_required", category=tr("categories.appointments")))

st.divider()

# 2. Notes, Elder Questions & Doctor Brief
if ConsentCategory.DOCTOR_BRIEF in actor.consents:
    st.subheader(f"📝 {tr('appointments.notes_heading')}")
    notes = container.brief.list_notes(actor)
    for n in notes:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**[{n.author_name}]** ({n.note_type}): {n.content}")
            with c2:
                can_del = actor.role == Role.ELDER
                if not can_del:
                    try:
                        authorize(
                            actor,
                            Action.DELETE_OWN_NOTE,
                            target_circle_id=actor.circle_id,
                            author_account_id=n.author_account_id,
                        )
                        can_del = True
                    except CareCircleError:
                        can_del = False
                if can_del and st.button(tr("common.delete"), key=f"del_note_{n.id}"):
                    try:
                        container.brief.delete_note(actor, note_id=n.id)
                        st.rerun()
                    except CareCircleError as exc:
                        render_error(exc)

    if actor.role == Role.ELDER:
        q_txt = st.text_input(tr("appointments.add_question_label"))
        if st.button(tr("common.save"), key="add_elder_q") and q_txt:
            try:
                container.brief.add_note(actor, note_type="elder_question", content=q_txt)
                st.rerun()
            except CareCircleError as exc:
                render_error(exc)
    else:
        try:
            authorize(actor, Action.ADD_OWN_NOTE, target_circle_id=actor.circle_id)
            n_txt = st.text_input(tr("appointments.add_note_label"))
            if st.button(tr("common.save"), key="add_fam_n") and n_txt:
                container.brief.add_note(actor, note_type="family_note", content=n_txt)
                st.rerun()
        except CareCircleError:
            pass

    st.divider()
    st.subheader(f"📄 {tr('appointments.brief_heading')}")
    render_banner("ℹ️", tr("appointments.brief_disclaimer"))
    st.caption(tr("appointments.pdf_recall_notice"))

    try:
        authorize(
            actor,
            Action.PREPARE_DOCTOR_BRIEF,
            target_circle_id=actor.circle_id,
            required_categories=(
                ConsentCategory.DOCTOR_BRIEF,
                ConsentCategory.MEDICATIONS,
                ConsentCategory.ADHERENCE,
                ConsentCategory.CHECKINS,
            ),
        )
        p_days = st.slider(
            tr("appointments.brief_period_label"), min_value=7, max_value=90, value=30
        )
        if st.button(tr("appointments.generate_brief_button"), type="primary"):
            container.brief.generate_brief(
                actor,
                period_days=p_days,
                use_ai_narrative=actor.ai_consent_granted and container.gemini_gateway.enabled,
            )
            st.rerun()
    except CareCircleError as exc:
        st.caption(tr(exc.locale_key, **exc.params))

    briefs = container.brief.list_accessible_briefs(actor)
    circle = container.circle.get_circle(actor)
    members = container.circle.list_members(actor)
    for b in briefs[:3]:
        with st.container(border=True):
            st.write(f"**v{b.version}** | {b.period_start} .. {b.period_end} ({b.timezone})")
            st.json(b.sections)
            if actor.role == Role.ELDER:
                non_elders = [(m, a) for m, a, _ in members if m.role != Role.ELDER and m.active]
                selected = st.multiselect(
                    tr("circle.members_heading"),
                    options=[m.id for m, _ in non_elders],
                    default=list(b.approved_recipient_membership_ids),
                    format_func=lambda mid, opts=non_elders: next(
                        (a.display_name for m, a in opts if m.id == mid), str(mid)
                    ),
                    key=f"brief_recips_{b.id}",
                )
                if st.button(tr("appointments.approve_brief_button"), key=f"approve_brief_{b.id}"):
                    container.brief.approve_brief(
                        actor,
                        brief_id=b.id,
                        expected_content_hash=b.content_hash,
                        recipient_membership_ids=selected,
                    )
                    st.success(tr("appointments.brief_approved", version=b.version))
                    st.rerun()

            try:
                exportable = container.brief.get_brief_for_export(actor, brief_id=b.id)
                en_pdf = render_brief_pdf(exportable, elder_name=circle.display_name, lang="en")
                hi_pdf = render_brief_pdf(exportable, elder_name=circle.display_name, lang="hi")
                p1, p2 = st.columns(2)
                with p1:
                    st.download_button(
                        tr("appointments.download_pdf_en"),
                        data=en_pdf,
                        file_name=f"carecircle-brief-v{b.version}-en.pdf",
                        mime="application/pdf",
                        key=f"dl_en_{b.id}",
                    )
                with p2:
                    st.download_button(
                        tr("appointments.download_pdf_hi"),
                        data=hi_pdf,
                        file_name=f"carecircle-brief-v{b.version}-hi.pdf",
                        mime="application/pdf",
                        key=f"dl_hi_{b.id}",
                    )
            except CareCircleError:
                st.caption(tr("errors.forbidden"))
else:
    st.info(tr("errors.consent_required", category=tr("categories.doctor_brief")))
