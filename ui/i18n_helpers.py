"""UI-layer localization helpers and friendly error mapping."""

from __future__ import annotations

from typing import Any

import streamlit as st
from core.errors import CareCircleError
from core.i18n import (
    SUPPORTED_LANGUAGES,
    format_local_date,
    format_local_datetime,
    format_local_time,
    t,
)


def current_lang() -> str:
    """Return active UI language from session state."""
    lang = str(st.session_state.get("ui_lang", "en"))
    return lang if lang in SUPPORTED_LANGUAGES else "en"


def tr(key: str, **params: Any) -> str:
    """Translate a locale key in the active UI language."""
    return t(key, lang=current_lang(), **params)


def format_dt(dt: Any, tz_name: str) -> str:
    """Format datetime in active language and circle timezone."""
    return format_local_datetime(dt, tz_name, lang=current_lang())


def format_tm(dt: Any, tz_name: str) -> str:
    """Format time in active language and circle timezone."""
    return format_local_time(dt, tz_name, lang=current_lang())


def format_d(d: Any, tz_name: str) -> str:
    """Format date in active language and circle timezone."""
    return format_local_date(d, tz_name, lang=current_lang())


def render_error(exc: Exception) -> None:
    """Display a friendly localized error without ever exposing raw tracebacks or DB messages."""
    if isinstance(exc, CareCircleError):
        st.error(f"⚠ {tr(exc.locale_key, **exc.params)}")
    else:
        st.error(f"⚠ {tr('errors.internal')}")
