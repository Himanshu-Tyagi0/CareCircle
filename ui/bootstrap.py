"""Shared Streamlit bootstrap: cached service container, session revalidation, and sidebar."""

from __future__ import annotations

import streamlit as st
from core.config import load_config
from core.errors import CareCircleError
from core.models import ActionTokenType, ActorContext, CantTakeReason, Role
from services.container import ServiceContainer, build_container

from ui.components import render_banner
from ui.i18n_helpers import current_lang, render_error, tr
from ui.styles import DEFAULT_ELDER_FONT_PX, DEFAULT_STANDARD_FONT_PX, build_css


@st.cache_resource
def get_container() -> ServiceContainer:
    """Create and cache stateless ServiceContainer (safe across concurrent users)."""
    cfg = load_config()
    return build_container(cfg)


def capture_query_tokens() -> None:
    """Capture query-string tokens into session state and clear visible URL promptly."""
    params = st.query_params
    changed = False
    for param_name, state_key in (
        ("verify", "pending_verify_token"),
        ("invite", "pending_invite_token"),
        ("action_token", "pending_action_token"),
    ):
        if param_name in params:
            val = params.get(param_name)
            if isinstance(val, list):
                val = val[0] if val else ""
            if val:
                st.session_state[state_key] = str(val)
            changed = True
    if changed:
        st.query_params.clear()


def apply_accessible_theme(actor: ActorContext | None) -> None:
    """Inject validated accessible CSS from persisted or session preferences."""
    if actor is not None:
        st.session_state["ui_lang"] = actor.language
        font_px = actor.font_px
        high_contrast = actor.high_contrast
    else:
        font_px = int(st.session_state.get("guest_font_px", DEFAULT_STANDARD_FONT_PX))
        high_contrast = bool(st.session_state.get("guest_high_contrast", False))
    st.markdown(build_css(font_px, high_contrast), unsafe_allow_html=True)


def render_link_flows(container: ServiceContainer) -> bool:
    """Render harmless-GET confirmation screens for verification, invitation, or action tokens."""
    if st.session_state.get("pending_verify_token"):
        raw = str(st.session_state["pending_verify_token"])
        st.header(tr("auth.verification_preview_heading"))
        render_banner("ℹ️", tr("action_page.harmless_note"))
        try:
            preview = container.auth.preview_verification(raw)
            st.write(f"**{preview['display_name']}** ({preview['timezone']})")
            st.write(tr("auth.verification_preview_body"))
            col1, col2 = st.columns(2)
            with col1:
                if st.button(tr("auth.verification_confirm_button"), type="primary"):
                    container.auth.confirm_verification(raw)
                    st.session_state.pop("pending_verify_token", None)
                    st.success(tr("auth.verification_complete"))
            with col2:
                if st.button(tr("common.cancel")):
                    st.session_state.pop("pending_verify_token", None)
                    st.rerun()
        except CareCircleError as exc:
            render_error(exc)
            if st.button(tr("common.back")):
                st.session_state.pop("pending_verify_token", None)
                st.rerun()
        return True

    if st.session_state.get("pending_invite_token"):
        raw = str(st.session_state["pending_invite_token"])
        st.header(tr("auth.invite_preview_heading"))
        render_banner("ℹ️", tr("action_page.harmless_note"))
        try:
            prev = container.circle.preview_invitation(raw)
            role_lbl = tr(f"roles.{prev['role'].value}")
            st.write(tr("auth.invite_preview_body", role=role_lbl, circle_name=prev["circle_name"]))
            if prev["existing_account"]:
                st.info(tr("auth.invite_existing_account_help"))
            else:
                st.info(tr("auth.invite_new_account_help"))
            with st.form("invite_accept_form"):
                disp_name = ""
                if not prev["existing_account"]:
                    disp_name = st.text_input(tr("auth.display_name_label"))
                passcode = st.text_input(tr("auth.passcode_label"), type="password")
                submitted = st.form_submit_button(tr("auth.invite_confirm_button"), type="primary")
            if submitted:
                container.circle.accept_invitation(
                    raw_token=raw, passcode=passcode, display_name=disp_name
                )
                st.session_state.pop("pending_invite_token", None)
                st.success(tr("auth.invite_accepted"))
            if st.button(tr("common.cancel")):
                st.session_state.pop("pending_invite_token", None)
                st.rerun()
        except CareCircleError as exc:
            render_error(exc)
            if st.button(tr("common.back")):
                st.session_state.pop("pending_invite_token", None)
                st.rerun()
        return True

    if st.session_state.get("pending_action_token"):
        raw = str(st.session_state["pending_action_token"])
        st.header(tr("action_page.heading"))
        render_banner("ℹ️", tr("action_page.harmless_note"))
        try:
            prev = container.action_tokens.preview_token(raw)
            if prev.resource_type == "dose":
                ctx = prev.context
                due_txt = str(ctx.get("due_at", ""))
                st.write(
                    tr(
                        "action_page.dose_context",
                        med=ctx.get("medication_name", ""),
                        dose=ctx.get("dose_text", ""),
                        due=due_txt,
                    )
                )
            else:
                st.write(tr("action_page.ack_context", elder=prev.elder_name))

            reason: CantTakeReason | None = None
            note = ""
            if prev.action == ActionTokenType.DOSE_CANT_TAKE:
                reason = st.selectbox(
                    tr("elder_mode.cant_take_reason_label"),
                    options=list(CantTakeReason),
                    format_func=lambda r: tr(f"cant_take_reasons.{r.value}"),
                )
                note = st.text_input(tr("elder_mode.cant_take_note_label"))

            col1, col2 = st.columns(2)
            with col1:
                if st.button(tr("action_page.confirm_button"), type="primary"):
                    container.action_tokens.redeem_token(
                        raw, cant_take_reason=reason, cant_take_note=note
                    )
                    st.session_state.pop("pending_action_token", None)
                    st.success(tr("action_page.completed"))
            with col2:
                if st.button(tr("common.cancel")):
                    st.session_state.pop("pending_action_token", None)
                    st.rerun()
        except CareCircleError as exc:
            render_error(exc)
            if st.button(tr("common.back")):
                st.session_state.pop("pending_action_token", None)
                st.rerun()
        return True
    return False


def render_sidebar_controls(container: ServiceContainer, actor: ActorContext | None) -> None:
    """Render sidebar language, text size, contrast, and sign-out controls."""
    with st.sidebar:
        st.title(f"🩺 {tr('app.title')}")
        lang_choice = st.radio(
            tr("common.language"),
            options=["en", "hi"],
            index=0 if current_lang() == "en" else 1,
            format_func=lambda c: tr("common.english") if c == "en" else tr("common.hindi"),
            horizontal=True,
        )
        if actor is not None:
            if lang_choice != actor.language:
                container.auth.update_own_preferences(
                    actor,
                    language=lang_choice,
                    font_px=actor.font_px,
                    high_contrast=actor.high_contrast,
                )
                st.session_state["ui_lang"] = lang_choice
                st.rerun()

            st.caption(
                tr(
                    "auth.signed_in_as",
                    name=actor.display_name,
                    role=tr(f"roles.{actor.role.value}"),
                )
            )
            st.caption(tr("common.timezone_label", tz=actor.circle_timezone))

            st.write(tr("common.text_size", px=actor.font_px))
            c1, c2 = st.columns(2)
            with c1:
                if st.button("A-", help=tr("common.smaller_text")) and actor.font_px > 18:
                    container.auth.update_own_preferences(
                        actor,
                        language=actor.language,
                        font_px=max(18, actor.font_px - 2),
                        high_contrast=actor.high_contrast,
                    )
                    st.rerun()
            with c2:
                if st.button("A+", help=tr("common.larger_text")) and actor.font_px < 32:
                    container.auth.update_own_preferences(
                        actor,
                        language=actor.language,
                        font_px=min(32, actor.font_px + 2),
                        high_contrast=actor.high_contrast,
                    )
                    st.rerun()

            hc = st.checkbox(tr("common.high_contrast"), value=actor.high_contrast)
            if hc != actor.high_contrast:
                container.auth.update_own_preferences(
                    actor,
                    language=actor.language,
                    font_px=actor.font_px,
                    high_contrast=hc,
                )
                st.rerun()

            if st.button(tr("nav.sign_out"), use_container_width=True):
                container.auth.logout(st.session_state.get("session_token"))
                st.session_state.pop("session_token", None)
                st.rerun()
        else:
            if lang_choice != current_lang():
                st.session_state["ui_lang"] = lang_choice
                st.rerun()
            g_px = int(st.session_state.get("guest_font_px", DEFAULT_STANDARD_FONT_PX))
            g_hc = bool(st.session_state.get("guest_high_contrast", False))
            st.write(tr("common.text_size", px=g_px))
            c1, c2 = st.columns(2)
            with c1:
                if st.button("A-", key="guest_a_minus") and g_px > 18:
                    st.session_state["guest_font_px"] = max(18, g_px - 2)
                    st.rerun()
            with c2:
                if st.button("A+", key="guest_a_plus") and g_px < 32:
                    st.session_state["guest_font_px"] = min(32, g_px + 2)
                    st.rerun()
            new_hc = st.checkbox(tr("common.high_contrast"), value=g_hc, key="guest_hc")
            if new_hc != g_hc:
                st.session_state["guest_high_contrast"] = new_hc
                st.rerun()


def bootstrap_page(*, require_auth: bool = True) -> tuple[ServiceContainer, ActorContext | None]:
    """Initialize page config, container, query tokens, session revalidation, and banners."""
    capture_query_tokens()
    try:
        container = get_container()
    except CareCircleError as exc:
        apply_accessible_theme(None)
        render_error(exc)
        st.stop()

    actor = container.auth.resolve_session(st.session_state.get("session_token"))
    if st.session_state.get("session_token") and actor is None:
        st.session_state.pop("session_token", None)

    if actor is not None and "ui_lang" not in st.session_state:
        st.session_state["ui_lang"] = actor.language

    apply_accessible_theme(actor)
    render_sidebar_controls(container, actor)

    if container.config.demo_mode:
        render_banner("🧪", tr("app.demo_banner"))

    health = container.repos.outbox.get_worker_health("reminders")
    if health.last_success_at is not None:
        lag_min = int((container.clock.now() - health.last_success_at).total_seconds() // 60)
        if lag_min >= container.config.worker_stale_minutes:
            render_banner("⏱", tr("app.stale_worker_banner", minutes=lag_min), role="alert")

    if render_link_flows(container):
        st.stop()

    if require_auth and actor is None:
        st.info(tr("auth.sign_in_help"))
        st.page_link("app.py", label=tr("auth.sign_in_button"))
        st.stop()

    return container, actor


def render_elder_default_font_notice(actor: ActorContext) -> int:
    """Return expected default font size for elder vs standard mode."""
    return DEFAULT_ELDER_FONT_PX if actor.role == Role.ELDER else DEFAULT_STANDARD_FONT_PX
