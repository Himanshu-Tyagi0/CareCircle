"""Elder-controlled Care Circle membership, invitations, consent matrix, policies, and audit log."""

from __future__ import annotations

import streamlit as st
from core.errors import CareCircleError
from core.models import ConsentCategory, FamilyGrant, Role
from core.permissions import Action, authorize
from services.circle_service import EMERGENCY_NUMBERS_BY_COUNTRY
from ui.bootstrap import bootstrap_page
from ui.components import render_accessible_table
from ui.i18n_helpers import format_dt, render_error, tr

st.set_page_config(page_title="CareCircle — Circle & Consent", page_icon="🛡", layout="wide")
container, actor = bootstrap_page(require_auth=True)
assert actor is not None

st.title(f"🛡 {tr('circle.heading')}")

try:
    authorize(actor, Action.MANAGE_CONSENT, target_circle_id=actor.circle_id)
except CareCircleError as exc:
    render_error(exc)
    st.stop()

circle = container.circle.get_circle(actor)
members = container.circle.list_members(actor)

# 1. Members & Granular Consent Matrix
st.subheader(tr("circle.members_heading"))
for mem, acc, cons_list in members:
    if mem.role == Role.ELDER:
        st.write(f"👑 **{acc.display_name}** ({tr('roles.elder')})")
        continue
    if not mem.active:
        continue
    granted_map = {c.category: c.granted for c in cons_list}
    with st.container(border=True):
        st.markdown(f"### {acc.display_name} — {tr(f'roles.{mem.role.value}')}")
        cols = st.columns(len(ConsentCategory))
        for idx, cat in enumerate(ConsentCategory):
            with cols[idx]:
                cur_val = granted_map.get(cat, False)
                new_val = st.checkbox(
                    tr(f"categories.{cat.value}"),
                    value=cur_val,
                    key=f"cons_{mem.id}_{cat.value}",
                )
                if new_val != cur_val:
                    container.circle.update_member_consent(
                        actor, membership_id=mem.id, category=cat, granted=new_val
                    )
                    st.success(tr("circle.consent_saved"))
                    st.rerun()

        care_contact = st.checkbox(
            tr("circle.invite_care_contact"),
            value=mem.is_care_contact,
            key=f"cc_flag_{mem.id}",
        )
        new_grants = mem.grants
        if mem.role == Role.FAMILY:
            selected_grants = st.multiselect(
                tr("roles.family"),
                options=list(FamilyGrant),
                default=list(mem.grants),
                format_func=lambda g: tr(f"grants.{g.value}"),
                key=f"grants_{mem.id}",
            )
            new_grants = frozenset(selected_grants)
        b1, b2 = st.columns(2)
        with b1:
            if st.button(tr("common.save"), key=f"save_grants_{mem.id}"):
                container.circle.update_member_grants(
                    actor,
                    membership_id=mem.id,
                    grants=new_grants,
                    is_care_contact=care_contact,
                )
                st.success(tr("circle.consent_saved"))
                st.rerun()
        with b2:
            if st.button(tr("common.delete"), key=f"remove_mem_{mem.id}"):
                container.circle.remove_member(actor, membership_id=mem.id)
                st.rerun()

# 2. Invite Member
st.divider()
st.subheader(tr("circle.invite_heading"))
with st.form("invite_form"):
    inv_email = st.text_input(tr("auth.email_label"))
    inv_role = st.selectbox(
        tr("circle.invite_role_label"),
        options=[Role.CARETAKER, Role.FAMILY],
        format_func=lambda r: tr(f"roles.{r.value}"),
    )
    inv_cc = st.checkbox(tr("circle.invite_care_contact"), value=True)
    inv_consents = st.multiselect(
        tr("circle.members_heading"),
        options=list(ConsentCategory),
        format_func=lambda c: tr(f"categories.{c.value}"),
    )
    inv_sub = st.form_submit_button(tr("circle.invite_button"), type="primary")
if inv_sub:
    try:
        tok = container.circle.create_invitation(
            actor,
            invited_email=inv_email,
            role=inv_role,
            initial_consents=frozenset(inv_consents),
            is_care_contact=inv_cc,
        )
        st.success(tr("circle.invite_created", email=inv_email))
        if container.config.demo_mode:
            st.session_state["demo_invite_token"] = tok
    except CareCircleError as exc:
        render_error(exc)

if (
    container.config.demo_mode
    and st.session_state.get("demo_invite_token")
    and st.button(tr("auth.invite_preview_heading"), key="open_demo_invite")
):
    st.session_state["pending_invite_token"] = st.session_state.pop("demo_invite_token")
    st.rerun()

# 3. Policies: Quiet hours, Check-ins, Contacts, Emergency, AI Consent
st.divider()
st.subheader(tr("circle.policies_heading"))
non_elders = [(m, a) for m, a, _ in members if m.role != Role.ELDER and m.active]
member_ids = [None] + [m.id for m, _ in non_elders]


def _fmt_mem(mid: object) -> str:
    if mid is None:
        return tr("common.not_configured")
    return next((a.display_name for m, a in non_elders if m.id == mid), str(mid))


with st.form("policies_form"):
    qh_on = st.checkbox(tr("circle.quiet_hours_enabled"), value=circle.quiet_hours_enabled)
    q1, q2 = st.columns(2)
    with q1:
        qh_start = st.time_input(tr("circle.quiet_start"), value=circle.quiet_hours_start)
    with q2:
        qh_end = st.time_input(tr("circle.quiet_end"), value=circle.quiet_hours_end)
    eq_rem = st.checkbox(
        tr("circle.elder_quiet_reminders"), value=circle.elder_quiet_reminders_enabled
    )

    chk_on = st.checkbox(tr("circle.checkin_enabled"), value=circle.checkin_enabled)
    chk_t = st.time_input(tr("circle.checkin_time"), value=circle.checkin_time)
    chk_win = st.number_input(
        tr("circle.checkin_window"),
        min_value=15,
        max_value=720,
        value=circle.checkin_window_minutes,
    )

    p_idx = (
        member_ids.index(circle.primary_contact_membership_id)
        if circle.primary_contact_membership_id in member_ids
        else 0
    )
    s_idx = (
        member_ids.index(circle.secondary_contact_membership_id)
        if circle.secondary_contact_membership_id in member_ids
        else 0
    )
    f_idx = (
        member_ids.index(circle.checkin_fallback_membership_id)
        if circle.checkin_fallback_membership_id in member_ids
        else 0
    )

    p_sel = st.selectbox(
        tr("circle.primary_contact"), options=member_ids, index=p_idx, format_func=_fmt_mem
    )
    s_sel = st.selectbox(
        tr("circle.secondary_contact"), options=member_ids, index=s_idx, format_func=_fmt_mem
    )
    f_sel = st.selectbox(
        tr("circle.checkin_fallback"), options=member_ids, index=f_idx, format_func=_fmt_mem
    )

    em_codes = [""] + sorted(EMERGENCY_NUMBERS_BY_COUNTRY.keys())
    em_idx = (
        em_codes.index(circle.emergency_country_code)
        if circle.emergency_country_code in em_codes
        else 0
    )
    em_country = st.selectbox(tr("circle.emergency_country"), options=em_codes, index=em_idx)
    em_name = st.text_input(tr("circle.emergency_name"), value=circle.emergency_contact_name or "")
    em_phone = st.text_input(
        tr("circle.emergency_phone"), value=circle.emergency_contact_phone or ""
    )

    st.info(tr("circle.ai_consent_disclosure"))
    ai_on = st.checkbox(tr("circle.ai_consent_label"), value=circle.ai_consent_granted)

    save_pol = st.form_submit_button(tr("common.save"), type="primary")

if save_pol:
    try:
        container.circle.update_policies(
            actor,
            quiet_hours_enabled=qh_on,
            quiet_hours_start=qh_start,
            quiet_hours_end=qh_end,
            elder_quiet_reminders_enabled=eq_rem,
            checkin_enabled=chk_on,
            checkin_time=chk_t,
            checkin_window_minutes=int(chk_win),
            primary_contact_membership_id=p_sel,
            secondary_contact_membership_id=s_sel,
            clear_secondary_contact=(s_sel is None),
            checkin_fallback_membership_id=f_sel,
            clear_checkin_fallback=(f_sel is None),
            emergency_country_code=em_country,
            emergency_contact_name=em_name,
            emergency_contact_phone=em_phone,
            ai_consent_granted=ai_on,
        )
        st.success(tr("circle.consent_saved"))
        st.rerun()
    except CareCircleError as exc:
        render_error(exc)

# 4. Audit Log
st.divider()
st.subheader(tr("circle.audit_heading"))
audit_rows = [
    [format_dt(ev.occurred_at, circle.timezone), ev.operation, ev.target_type, ev.target_id]
    for ev in container.circle.list_audit_log(actor, limit=30)
]
if audit_rows:
    render_accessible_table(
        headers=["Time", "Operation", "Target", "ID"],
        rows=audit_rows,
        caption=tr("circle.audit_heading"),
    )

# 5. Circle Deletion
st.divider()
with st.expander(tr("circle.delete_circle_heading")):
    st.warning(tr("circle.delete_circle_warning"))
    del_txt = st.text_input(tr("circle.delete_circle_confirm_label"))
    if st.button(tr("circle.delete_circle_button")):
        try:
            container.circle.delete_circle(actor, confirmation_text=del_txt)
            st.session_state.pop("session_token", None)
            st.rerun()
        except CareCircleError as exc:
            render_error(exc)
