"""CareCircle main Streamlit entry: sign-in, verified elder onboarding, and home routing."""

from __future__ import annotations

import streamlit as st
from core.errors import CareCircleError
from core.models import Role
from scripts.seed_demo import DEMO_ACCOUNTS
from ui.bootstrap import bootstrap_page
from ui.i18n_helpers import current_lang, render_error, tr

st.set_page_config(page_title="CareCircle", page_icon="🩺", layout="wide")

container, actor = bootstrap_page(require_auth=False)

if actor is not None:
    st.title(f"🩺 {tr('app.title')} — {tr('elder_mode.greeting', name=actor.display_name)}")
    st.write(tr("app.subtitle"))
    if actor.role == Role.ELDER:
        st.page_link("pages/01_Elder_Mode.py", label=f"🏡 {tr('nav.elder_mode')}", icon="🩺")
    st.page_link("pages/01_Elder_Mode.py", label=tr("nav.elder_mode"))
    st.page_link("pages/02_Medications_And_Schedule.py", label=tr("nav.medications"))
    st.page_link("pages/03_Adherence_And_History.py", label=tr("nav.adherence"))
    st.page_link("pages/04_Appointments_And_Brief.py", label=tr("nav.appointments_brief"))
    st.page_link("pages/05_Care_Circle_And_Consent.py", label=tr("nav.circle_consent"))
    st.page_link("pages/06_Care_Chain_And_Worker.py", label=tr("nav.care_chain"))
    st.page_link("pages/07_Account_And_About.py", label=tr("nav.account_about"))
    st.stop()

st.title(f"🩺 {tr('app.title')}")
st.write(tr("app.subtitle"))

tab_signin, tab_register = st.tabs([tr("auth.sign_in_heading"), tr("auth.register_heading")])

with tab_signin:
    st.info(tr("auth.sign_in_help"))
    with st.form("signin_form"):
        email = st.text_input(tr("auth.email_label"))
        passcode = st.text_input(tr("auth.passcode_label"), type="password")
        submitted = st.form_submit_button(tr("auth.sign_in_button"), type="primary")
    if submitted:
        try:
            acc, memberships = container.auth.authenticate(email=email, passcode=passcode)
            if len(memberships) == 1:
                raw_sess, new_actor = container.auth.issue_session(
                    account_id=acc.id, membership_id=memberships[0][0].id
                )
                st.session_state["session_token"] = raw_sess
                st.session_state["ui_lang"] = new_actor.language
                if new_actor.role == Role.ELDER:
                    st.switch_page("pages/01_Elder_Mode.py")
                st.rerun()
            else:
                st.session_state["pending_circle_choices"] = (acc.id, memberships)
        except CareCircleError as exc:
            render_error(exc)

    if "pending_circle_choices" in st.session_state:
        acc_id, choices = st.session_state["pending_circle_choices"]
        st.subheader(tr("auth.select_circle_heading"))
        for mem, circle in choices:
            lbl = f"{circle.display_name} ({tr(f'roles.{mem.role.value}')})"
            if st.button(lbl, key=f"choose_circle_{mem.id}"):
                raw_sess, new_actor = container.auth.issue_session(
                    account_id=acc_id, membership_id=mem.id
                )
                st.session_state.pop("pending_circle_choices", None)
                st.session_state["session_token"] = raw_sess
                st.session_state["ui_lang"] = new_actor.language
                st.rerun()

    if container.config.demo_mode:
        st.divider()
        st.subheader(tr("auth.demo_quick_login_heading"))
        st.caption(tr("auth.demo_quick_login_help"))
        cols = st.columns(3)
        for idx, (d_email, d_name, d_lang, d_role, d_pass) in enumerate(DEMO_ACCOUNTS):
            with cols[idx % 3]:
                role_lbl = tr(f"roles.{d_role.value}")
                btn_lbl = f"{d_name} — {role_lbl} ({d_lang.upper()})"
                if st.button(btn_lbl, key=f"demo_login_{d_email}"):
                    try:
                        acc, memberships = container.auth.authenticate(
                            email=d_email, passcode=d_pass
                        )
                        raw_sess, new_actor = container.auth.issue_session(
                            account_id=acc.id, membership_id=memberships[0][0].id
                        )
                        st.session_state["session_token"] = raw_sess
                        st.session_state["ui_lang"] = new_actor.language
                        if new_actor.role == Role.ELDER:
                            st.switch_page("pages/01_Elder_Mode.py")
                        st.rerun()
                    except CareCircleError as exc:
                        render_error(exc)

with tab_register:
    st.info(tr("auth.register_help"))
    with st.form("register_elder_form"):
        reg_name = st.text_input(tr("auth.display_name_label"))
        reg_email = st.text_input(tr("auth.email_label"))
        reg_tz = st.selectbox(
            tr("auth.timezone_label"),
            options=[
                "Asia/Kolkata",
                "America/New_York",
                "America/Los_Angeles",
                "Europe/London",
                "UTC",
            ],
        )
        reg_pass = st.text_input(tr("auth.passcode_label"), type="password")
        reg_sub = st.form_submit_button(tr("auth.register_button"), type="primary")
    if reg_sub:
        try:
            demo_tok = container.auth.start_elder_onboarding(
                email=reg_email,
                display_name=reg_name,
                passcode=reg_pass,
                timezone=reg_tz,
                language=current_lang(),
            )
            st.success(tr("auth.verification_sent"))
            if demo_tok and container.config.demo_mode:
                st.session_state["demo_verify_token"] = demo_tok
        except CareCircleError as exc:
            render_error(exc)

    if (
        container.config.demo_mode
        and st.session_state.get("demo_verify_token")
        and st.button(tr("auth.demo_verify_preview_link"), type="primary")
    ):
        st.session_state["pending_verify_token"] = st.session_state.pop("demo_verify_token")
        st.rerun()
