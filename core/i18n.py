"""Bilingual English/Hindi localization loader, strict validator, and timezone formatters."""

from __future__ import annotations

import json
import string
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SUPPORTED_LANGUAGES = ("en", "hi")
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, val in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            flat.update(_flatten(val, full))
        else:
            flat[full] = str(val)
    return flat


def _extract_placeholders(template: str) -> set[str]:
    formatter = string.Formatter()
    names: set[str] = set()
    for _, field_name, _, _ in formatter.parse(template):
        if field_name is not None:
            names.add(field_name.split(".")[0].split("[")[0])
    return names


@lru_cache(maxsize=4)
def load_catalog(lang: str) -> dict[str, str]:
    """Load and flatten a locale JSON catalog without importing Streamlit."""
    code = lang if lang in SUPPORTED_LANGUAGES else "en"
    path = _LOCALES_DIR / f"{code}.json"
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return _flatten(raw)


def validate_locale_parity() -> list[str]:
    """Return a list of missing keys or mismatched placeholders between en and hi."""
    en = load_catalog("en")
    hi = load_catalog("hi")
    issues: list[str] = []
    for key in sorted(set(en.keys()) - set(hi.keys())):
        issues.append(f"Missing in hi.json: {key}")
    for key in sorted(set(hi.keys()) - set(en.keys())):
        issues.append(f"Missing in en.json: {key}")
    for key in sorted(set(en.keys()) & set(hi.keys())):
        en_ph = _extract_placeholders(en[key])
        hi_ph = _extract_placeholders(hi[key])
        if en_ph != hi_ph:
            issues.append(f"Placeholder mismatch for {key}: en={sorted(en_ph)} hi={sorted(hi_ph)}")
    return issues


def t(key: str, *, lang: str = "en", strict: bool = False, **params: Any) -> str:
    """Translate a locale key using explicit language and named parameters."""
    catalog = load_catalog(lang)
    template = catalog.get(key)
    if template is None:
        if strict:
            raise KeyError(f"Missing locale key '{key}' for language '{lang}'")
        template = load_catalog("en").get(key)
        if template is None:
            return key
    try:
        return template.format(**params)
    except (KeyError, ValueError) as exc:
        if strict:
            raise ValueError(f"Invalid format params for '{key}' ({lang}): {exc}") from exc
        return template


def format_local_datetime(dt: datetime, timezone_name: str, *, lang: str = "en") -> str:
    """Format a UTC datetime in the target IANA timezone and language."""
    local = dt.astimezone(ZoneInfo(timezone_name))
    if lang == "hi":
        ampm = "पूर्वाह्न" if local.hour < 12 else "अपराह्न"
        hour12 = local.hour % 12 or 12
        return f"{local.strftime('%d/%m/%Y')} {hour12}:{local.minute:02d} {ampm}"
    return local.strftime("%d %b %Y, %I:%M %p")


def format_local_time(dt: datetime, timezone_name: str, *, lang: str = "en") -> str:
    """Format only the local time component in the target IANA timezone."""
    local = dt.astimezone(ZoneInfo(timezone_name))
    if lang == "hi":
        ampm = "पूर्वाह्न" if local.hour < 12 else "अपराह्न"
        hour12 = local.hour % 12 or 12
        return f"{hour12}:{local.minute:02d} {ampm}"
    return local.strftime("%I:%M %p")


def format_local_date(d: date | datetime, timezone_name: str, *, lang: str = "en") -> str:
    """Format a date or datetime's local date in the target IANA timezone."""
    if isinstance(d, datetime):
        d = d.astimezone(ZoneInfo(timezone_name)).date()
    return d.strftime("%d/%m/%Y") if lang == "hi" else d.strftime("%d %b %Y")
