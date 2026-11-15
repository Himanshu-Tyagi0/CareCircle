"""Accessible UI components: status banners, tables, and safe browser read-aloud."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st
import streamlit.components.v1 as components
from core.security import escape_html

from ui.i18n_helpers import tr


def render_banner(icon: str, text: str, *, role: str = "status") -> None:
    """Render an accessible persistent status banner with icon + text (never color alone)."""
    safe_text = escape_html(text)
    safe_icon = escape_html(icon)
    aria = "alert" if role == "alert" else "status"
    st.markdown(
        f"<div class='cc-banner' role='{aria}'><strong>{safe_icon}</strong> {safe_text}</div>",
        unsafe_allow_html=True,
    )


def render_accessible_table(headers: list[str], rows: list[list[Any]], *, caption: str) -> None:
    """Render a semantic HTML table with caption and scope attributes for screen readers."""
    th_html = "".join(
        f"<th scope='col' style='text-align:left;padding:8px;border-bottom:2px solid #5A6B68;'>{escape_html(str(h))}</th>"
        for h in headers
    )
    body_rows: list[str] = []
    for row in rows:
        tds = "".join(
            f"<td style='padding:8px;border-bottom:1px solid #CBD5D3;'>{escape_html(str(cell))}</td>"
            for cell in row
        )
        body_rows.append(f"<tr>{tds}</tr>")
    table_html = (
        f"<table style='width:100%;border-collapse:collapse;'>"
        f"<caption style='text-align:left;font-weight:bold;margin-bottom:6px;'>{escape_html(caption)}</caption>"
        f"<thead><tr>{th_html}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def render_read_aloud_controls(text_to_read: str, *, lang: str) -> None:
    """Explicit user-gesture browser speech synthesis with Stop, Replay, and Hindi-voice fallback.

    Passes untrusted text via JSON inside a non-executable <script type='application/json'>
    element rather than string-interpolating into JavaScript.
    """
    payload = json.dumps(
        {
            "text": text_to_read,
            "lang": "hi-IN" if lang == "hi" else "en-IN",
            "readLabel": tr("common.read_aloud"),
            "stopLabel": tr("common.stop_reading"),
            "replayLabel": tr("common.replay_reading"),
            "hiMissing": tr("common.voice_hindi_unavailable"),
            "isHindi": lang == "hi",
        }
    ).replace("</", "<\\/")

    html_widget = f"""
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-family:sans-serif;">
  <script id="cc-tts-payload" type="application/json">{payload}</script>
  <button id="cc-read-btn" type="button" style="min-height:48px;padding:10px 18px;font-size:18px;font-weight:600;border-radius:8px;border:2px solid #0F5C54;background:#0F5C54;color:#fff;cursor:pointer;"></button>
  <button id="cc-stop-btn" type="button" style="min-height:48px;padding:10px 18px;font-size:18px;font-weight:600;border-radius:8px;border:2px solid #5A6B68;background:#EDF4F2;color:#0F3D38;cursor:pointer;"></button>
  <button id="cc-replay-btn" type="button" style="min-height:48px;padding:10px 18px;font-size:18px;font-weight:600;border-radius:8px;border:2px solid #5A6B68;background:#EDF4F2;color:#0F3D38;cursor:pointer;"></button>
  <span id="cc-tts-note" role="status" style="font-size:16px;color:#141A19;"></span>
</div>
<script>
(function() {{
  const cfg = JSON.parse(document.getElementById('cc-tts-payload').textContent);
  const readBtn = document.getElementById('cc-read-btn');
  const stopBtn = document.getElementById('cc-stop-btn');
  const replayBtn = document.getElementById('cc-replay-btn');
  const noteEl = document.getElementById('cc-tts-note');
  readBtn.textContent = "🔊 " + cfg.readLabel;
  stopBtn.textContent = "⏹ " + cfg.stopLabel;
  replayBtn.textContent = "🔁 " + cfg.replayLabel;

  function speakText() {{
    if (!('speechSynthesis' in window)) {{
      noteEl.textContent = cfg.hiMissing;
      return;
    }}
    window.speechSynthesis.cancel();
    const voices = window.speechSynthesis.getVoices ? window.speechSynthesis.getVoices() : [];
    if (cfg.isHindi && voices.length > 0 && !voices.some(v => (v.lang || '').toLowerCase().startsWith('hi'))) {{
      noteEl.textContent = cfg.hiMissing;
    }} else {{
      noteEl.textContent = '';
    }}
    const u = new SpeechSynthesisUtterance(cfg.text);
    u.lang = cfg.lang;
    window.speechSynthesis.speak(u);
  }}
  readBtn.addEventListener('click', speakText);
  replayBtn.addEventListener('click', speakText);
  stopBtn.addEventListener('click', () => {{
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  }});
}})();
</script>
"""
    components.html(html_widget, height=84)
