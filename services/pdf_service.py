"""English and Hindi Doctor Visit Brief PDF generation using fpdf2 + uharfbuzz."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.i18n import format_local_datetime, t
from core.models_clinical import DoctorBriefSnapshot
from fpdf import FPDF

_FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"


class _BriefPDF(FPDF):
    """FPDF subclass embedding NotoSans + NotoSansDevanagari with HarfBuzz shaping."""

    def __init__(self, *, lang: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self._lang = lang
        self.set_auto_page_break(auto=True, margin=14)
        self.set_margins(14, 14, 14)

        latin_reg = _FONTS_DIR / "NotoSans-Regular.ttf"
        latin_bold = _FONTS_DIR / "NotoSans-Bold.ttf"
        dev_reg = _FONTS_DIR / "NotoSansDevanagari-Regular.ttf"
        dev_bold = _FONTS_DIR / "NotoSansDevanagari-Bold.ttf"

        self.add_font("NotoSans", "", str(latin_reg))
        self.add_font("NotoSans", "B", str(latin_bold))
        self.add_font("NotoDeva", "", str(dev_reg))
        self.add_font("NotoDeva", "B", str(dev_bold))
        self.set_text_shaping(use_shaping_engine=True)
        if lang == "hi":
            self.set_font("NotoDeva", "", 11)
            self.set_fallback_fonts(["NotoSans"])
        else:
            self.set_font("NotoSans", "", 11)
            self.set_fallback_fonts(["NotoDeva"])


def _family_for(lang: str) -> str:
    return "NotoDeva" if lang == "hi" else "NotoSans"


def render_brief_pdf(brief: DoctorBriefSnapshot, *, elder_name: str, lang: str = "en") -> bytes:
    """Render a 1-page summary PDF (with clean overflow appendix if needed) in EN or HI."""
    code = "hi" if lang == "hi" else "en"
    fam = _family_for(code)
    pdf = _BriefPDF(lang=code)
    pdf.add_page()

    # Header
    pdf.set_font(fam, "B", 16)
    pdf.cell(
        0,
        9,
        f"{t('appointments.brief_heading', lang=code)} — {elder_name}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_font(fam, "", 9.5)
    gen_at = format_local_datetime(
        datetime.fromisoformat(str(brief.sections["generated_at_iso"])),
        brief.timezone,
        lang=code,
    )
    pdf.multi_cell(
        0,
        5.5,
        f"{brief.period_start.isoformat()} .. {brief.period_end.isoformat()} ({brief.timezone}) | v{brief.version} | {gen_at}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_font(fam, "B", 9.5)
    pdf.multi_cell(
        0,
        5.5,
        t("appointments.brief_disclaimer", lang=code),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    if code == "en" and brief.ai_narrative_en:
        pdf.set_font(fam, "", 10)
        pdf.multi_cell(0, 5.5, brief.ai_narrative_en, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    elif code == "hi" and brief.ai_narrative_hi:
        pdf.set_font(fam, "", 10)
        pdf.multi_cell(0, 5.5, brief.ai_narrative_hi, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # 1. Medications (bounded on Page 1; overflow goes to Appendix)
    meds = list(brief.sections.get("medications", []))
    pdf.set_font(fam, "B", 12)
    pdf.cell(
        0, 7, t("elder_mode.todays_medicines_heading", lang=code), new_x="LMARGIN", new_y="NEXT"
    )
    pdf.set_font(fam, "", 10)
    page1_meds = meds[:6]
    overflow_meds = meds[6:]
    for m in page1_meds:
        pdf.multi_cell(
            0,
            5.5,
            f"• {m['name']} — {m['dose']} ({m.get('instructions', '')})",
            new_x="LMARGIN",
            new_y="NEXT",
        )
    if not page1_meds:
        pdf.cell(0, 6, t("common.empty", lang=code), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # 2. Adherence Summary
    adh = brief.sections.get("adherence", {})
    pdf.set_font(fam, "B", 12)
    pdf.cell(0, 7, t("adherence.heading", lang=code), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(fam, "", 10)
    rate = adh.get("rate_percent")
    rate_txt = f"{rate}%" if rate is not None else t("common.unavailable", lang=code)
    cant_take_lbl = t("dose_states.can't_take", lang=code)
    pdf.multi_cell(
        0,
        5.5,
        f"{adh.get('taken', 0)}/{adh.get('total_past_due', 0)} ({rate_txt}) | "
        f"{cant_take_lbl}: {adh.get('cant_take', 0)} | "
        f"{t('dose_states.skipped', lang=code)}: {adh.get('skipped', 0)} | "
        f"{t('dose_states.expired', lang=code)}: {adh.get('unconfirmed', 0)}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    # 3. Check-in Trend
    chk = brief.sections.get("checkins", {})
    pdf.set_font(fam, "B", 12)
    pdf.cell(0, 7, t("categories.checkins", lang=code), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(fam, "", 10)
    pdf.multi_cell(
        0,
        5.5,
        f"{chk.get('responses', 0)} — {t('moods.good', lang=code)}: {chk.get('good', 0)}, "
        f"{t('moods.okay', lang=code)}: {chk.get('okay', 0)}, "
        f"{t('moods.not_well', lang=code)}: {chk.get('not_well', 0)}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    # 4. Notes & Questions (bounded on Page 1)
    notes = list(brief.sections.get("notes_and_questions", []))
    pdf.set_font(fam, "B", 12)
    pdf.cell(0, 7, t("appointments.notes_heading", lang=code), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(fam, "", 10)
    page1_notes = notes[:5]
    overflow_notes = notes[5:]
    for n in page1_notes:
        pdf.multi_cell(0, 5.5, f"• [{n['author']}] {n['content']}", new_x="LMARGIN", new_y="NEXT")
    if not page1_notes:
        pdf.cell(0, 6, t("common.empty", lang=code), new_x="LMARGIN", new_y="NEXT")

    # Appendix if any overflow
    patterns = list(brief.sections.get("patterns", []))
    if overflow_meds or overflow_notes or patterns:
        pdf.add_page()
        pdf.set_font(fam, "B", 13)
        appendix_title = "Appendix — Detailed Records" if code == "en" else "परिशिष्ट — विस्तृत रिकॉर्ड"
        pdf.cell(0, 8, appendix_title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(fam, "", 10)
        for m in overflow_meds:
            pdf.multi_cell(
                0,
                5.5,
                f"• {m['name']} — {m['dose']} ({m.get('instructions', '')})",
                new_x="LMARGIN",
                new_y="NEXT",
            )
        for n in overflow_notes:
            pdf.multi_cell(
                0, 5.5, f"• [{n['author']}] {n['content']}", new_x="LMARGIN", new_y="NEXT"
            )
        for p in patterns:
            pdf.multi_cell(
                0,
                5.5,
                f"• {p['date']} | {p['med']} | {p['state']} ({p.get('reason', '')})",
                new_x="LMARGIN",
                new_y="NEXT",
            )

    return bytes(pdf.output())
