"""Accessible CSS builder (validated numeric/boolean inputs only; zero user-text interpolation)."""

from __future__ import annotations

from core.errors import ValidationError

MIN_FONT_PX = 18
MAX_FONT_PX = 32
DEFAULT_ELDER_FONT_PX = 24
DEFAULT_STANDARD_FONT_PX = 18

# Pinned to streamlit==1.67.0 DOM selectors:
# - .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"]
# - .stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button
# - input, textarea, select, [data-baseweb="select"], [data-testid="stMetric"]
STREAMLIT_SELECTOR_VERSION = "1.67.0"


def build_css(font_px: int, high_contrast: bool = False) -> str:
    """Build accessible CareCircle CSS from strict integer font_px (18..32) and boolean contrast."""
    if isinstance(font_px, bool) or not isinstance(font_px, int):
        raise ValidationError("errors.validation")
    if not (MIN_FONT_PX <= font_px <= MAX_FONT_PX):
        raise ValidationError("errors.validation")
    if not isinstance(high_contrast, bool):
        raise ValidationError("errors.validation")

    heading_px = min(font_px + 10, 42)
    subheading_px = min(font_px + 4, 36)
    small_px = max(font_px - 2, 16)

    if high_contrast:
        bg = "#FFFFFF"
        surface = "#FFFFFF"
        text = "#000000"
        border = "#000000"
        primary_bg = "#000000"
        primary_fg = "#FFFFFF"
        secondary_bg = "#FFFFFF"
        secondary_fg = "#000000"
        focus_color = "#0033CC"
        card_shadow = "none"
    else:
        bg = "#F8F7F4"
        surface = "#FFFFFF"
        text = "#141A19"
        border = "#5A6B68"
        primary_bg = "#0F5C54"
        primary_fg = "#FFFFFF"
        secondary_bg = "#EDF4F2"
        secondary_fg = "#0F3D38"
        focus_color = "#0B4F48"
        card_shadow = "0 1px 3px rgba(20, 26, 25, 0.08)"

    return f"""
<style>
/* CareCircle Accessible Stylesheet (Streamlit {STREAMLIT_SELECTOR_VERSION}) */
:root {{
  --cc-font-px: {font_px}px;
  --cc-heading-px: {heading_px}px;
  --cc-subheading-px: {subheading_px}px;
  --cc-small-px: {small_px}px;
  --cc-bg: {bg};
  --cc-surface: {surface};
  --cc-text: {text};
  --cc-border: {border};
  --cc-primary-bg: {primary_bg};
  --cc-primary-fg: {primary_fg};
  --cc-secondary-bg: {secondary_bg};
  --cc-secondary-fg: {secondary_fg};
  --cc-focus: {focus_color};
}}

.stApp, [data-testid="stAppViewContainer"] {{
  background-color: var(--cc-bg);
  color: var(--cc-text);
  font-size: var(--cc-font-px);
  line-height: 1.5;
}}

.stApp p, .stApp li, .stApp label, .stApp span, .stApp div {{
  font-size: inherit;
}}

.stApp h1 {{
  font-size: var(--cc-heading-px) !important;
  color: var(--cc-text) !important;
  line-height: 1.25;
}}

.stApp h2, .stApp h3 {{
  font-size: var(--cc-subheading-px) !important;
  color: var(--cc-text) !important;
  line-height: 1.3;
}}

[data-testid="stSidebar"] {{
  background-color: var(--cc-surface);
  border-right: 2px solid var(--cc-border);
}}

.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button {{
  min-height: 48px !important;
  min-width: 48px !important;
  padding: 0.65rem 1.15rem !important;
  font-size: var(--cc-font-px) !important;
  font-weight: 600 !important;
  border-radius: 8px !important;
  border: 2px solid var(--cc-border) !important;
  margin-bottom: 0.35rem !important;
  background-color: var(--cc-secondary-bg) !important;
  color: var(--cc-secondary-fg) !important;
}}

.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {{
  background-color: var(--cc-primary-bg) !important;
  color: var(--cc-primary-fg) !important;
  border-color: var(--cc-primary-bg) !important;
}}

.stButton > button:focus-visible,
.stDownloadButton > button:focus-visible,
input:focus-visible,
textarea:focus-visible,
select:focus-visible,
a:focus-visible {{
  outline: 3px solid var(--cc-focus) !important;
  outline-offset: 2px !important;
}}

input, textarea, select, [data-baseweb="input"], [data-baseweb="select"] {{
  font-size: var(--cc-font-px) !important;
  min-height: 48px !important;
  color: var(--cc-text) !important;
  background-color: var(--cc-surface) !important;
}}

.cc-card {{
  background-color: var(--cc-surface);
  border: 2px solid var(--cc-border);
  border-radius: 10px;
  padding: 1rem 1.15rem;
  margin-bottom: 0.85rem;
  box-shadow: {card_shadow};
}}

.cc-banner {{
  border: 2px solid var(--cc-border);
  border-left-width: 8px;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  margin-bottom: 0.75rem;
  background-color: var(--cc-surface);
  color: var(--cc-text);
  font-size: var(--cc-small-px);
}}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: 0.001ms !important;
    transition-duration: 0.001ms !important;
  }}
}}

@media (max-width: 640px) {{
  .block-container {{
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
  }}
}}
</style>
""".strip()
