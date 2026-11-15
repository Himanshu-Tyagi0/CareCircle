"""Account settings, passcode change, Telegram opt-in, recovery policy, and localized About/Disclaimer."""

from __future__ import annotations

import streamlit as st
from core.errors import CareCircleError
from ui.bootstrap import bootstrap_page
from ui.components import render_banner
from ui.i18n_helpers import render_error, tr

st.set_page_config(page_title="CareCircle — Account & About", page_icon="ℹ", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"ℹ {tr('about.heading')}")

st.write(tr("about.mission"))
st.subheader(tr("about.medical_disclaimer_heading"))
render_banner("⚕", tr("about.medical_disclaimer_body"), role="alert")

st.divider()
st.subheader(tr("auth.change_passcode_heading"))
with st.form("change_passcode_form"):
    cur_p = st.text_input(tr("auth.current_passcode_label"), type="password")
    new_p = st.text_input(tr("auth.new_passcode_label"), type="password")
    sub_p = st.form_submit_button(tr("auth.change_passcode_button"), type="primary")
if sub_p:
    try:
        container.auth.change_own_passcode(
            actor,
            current_passcode=cur_p,
            new_passcode=new_p,
            current_raw_session_token=st.session_state.get("session_token"),
        )
        st.success(tr("auth.passcode_updated"))
    except CareCircleError as exc:
        render_error(exc)

st.subheader(tr("auth.recovery_heading"))
st.info(tr("auth.recovery_body"))

# Telegram individual opt-in
acc = container.repos.auth.get_account_by_id(actor.account_id)
if acc:
    with st.expander("Telegram Opt-in"):
        tg_on = st.checkbox("Enable Telegram notifications", value=acc.telegram_opt_in)
        tg_chat = st.text_input("Telegram Chat ID", value=acc.telegram_chat_id or "")
        if st.button(tr("common.save"), key="save_tg"):
            try:
                container.auth.update_own_telegram(actor, opt_in=tg_on, chat_id=tg_chat)
                st.rerun()
            except CareCircleError as exc:
                render_error(exc)

# Create own circle for non-elder accounts without one
with st.expander(tr("auth.create_circle_heading")):
    with st.form("create_own_circle_form"):
        c_name = st.text_input(tr("auth.display_name_label"), value=actor.display_name)
        c_tz = st.selectbox(
            tr("auth.timezone_label"),
            options=["Asia/Kolkata", "America/New_York", "Europe/London", "UTC"],
        )
        c_sub = st.form_submit_button(tr("auth.create_circle_button"))
    if c_sub:
        try:
            container.auth.create_own_circle(actor, display_name=c_name, timezone=c_tz)
            st.success(tr("auth.circle_created"))
        except CareCircleError as exc:
            render_error(exc)

st.divider()
st.subheader(tr("about.config_status_heading"))
st.json(container.config.safe_diagnostics())
