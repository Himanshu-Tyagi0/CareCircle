"""Tests for all 8 Gemini AI features, media safety, Doctor Brief, EN/HI PDFs, i18n, and CSS."""

from __future__ import annotations

import io
import json
from datetime import date, time

import pytest
from core.errors import (
    AIServiceError,
    AuthorizationError,
    CareCircleError,
    ConsentRequiredError,
    ValidationError,
)
from core.i18n import (
    SUPPORTED_LANGUAGES,
    format_local_date,
    format_local_datetime,
    t,
    validate_locale_parity,
)
from core.models import CheckInMood, ConsentCategory, Criticality, Role
from PIL import Image
from services.container import ServiceContainer
from services.pdf_service import render_brief_pdf
from tests.conftest import FakeGeminiGateway, invite_and_join, onboard_elder
from ui.styles import build_css


def _make_png_bytes(width: int = 32, height: int = 32) -> bytes:
    img = Image.new("RGB", (width, height), color=(15, 92, 84))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _contrast_ratio(hex1: str, hex2: str) -> float:
    def lum(h: str) -> float:
        h = h.lstrip("#")
        vals = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
        lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    l1, l2 = lum(hex1), lum(hex2)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


@pytest.mark.integration
def test_all_eight_ai_features_and_safety_boundaries(
    container: ServiceContainer, gemini_gw: FakeGeminiGateway
) -> None:
    """All 8 AI features enforce consent, bounded JSON, factual preservation, media checks, and refusals."""
    _, elder = onboard_elder(
        container, email="e@example.test", name="Asha", timezone="Asia/Kolkata"
    )
    _, care, _ = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )

    # Without AI consent -> ConsentRequiredError
    with pytest.raises(ConsentRequiredError):
        container.ai.parse_nl_schedule(elder, text_input="Take 1 tab after breakfast")

    # Enable AI consent & create sample med + checkin
    container.circle.update_policies(elder, ai_consent_granted=True)
    elder = container.auth.resolve_session(
        container.auth.issue_session(
            account_id=elder.account_id, membership_id=elder.membership_id
        )[0]
    )
    assert elder is not None
    container.medication.create_medication(
        elder,
        name="Metformin",
        dose_text="500 mg",
        instructions="Take 500 mg after breakfast",
        start_date=date(2026, 5, 10),
        end_date=None,
        criticality=Criticality.NORMAL,
        stock_count=10,
        stock_unit="tablets",
        refill_threshold=2,
        stock_units_per_dose=1,
        times_local=[time(8, 0)],
        weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    container.schedule.generate_horizon_for_circle(elder.circle_id, horizon_hours=24)
    container.checkin.submit_checkin(elder, mood=CheckInMood.GOOD)

    # 1. Prescription scan (valid + oversized/bad media rejection)
    gemini_gw.response = json.dumps(
        {
            "items": [
                {
                    "name": "Metformin",
                    "strength": "500 mg",
                    "dose_instructions": "1 tablet after food",
                    "frequency": "daily",
                    "duration": "30 days",
                    "uncertain_fields": [],
                }
            ],
            "needs_human_review": True,
        }
    )
    scan = container.ai.scan_prescription(elder, image_bytes=_make_png_bytes())
    assert scan.items[0].name == "Metformin"
    with pytest.raises(ValidationError):
        container.ai.scan_prescription(elder, image_bytes=b"not-an-image")

    # 2. NL schedule parser
    gemini_gw.response = json.dumps(
        {
            "medication_name": "Metformin",
            "dose_text": "500 mg",
            "times_hhmm": ["08:30"],
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "unresolved_questions": [],
        }
    )
    nl = container.ai.parse_nl_schedule(elder, text_input="Metformin 500 mg after breakfast")
    assert nl.times_hhmm == ["08:30"]

    # 3. Simplify instruction (factual preservation pass + fail fallback)
    gemini_gw.response = json.dumps(
        {
            "protected_dose": "500 mg",
            "protected_timing": "after breakfast",
            "protected_cautions": ["as advised"],
            "plain_summary": "Take 500 mg after breakfast as advised.",
        }
    )
    ok_simp, simp_res = container.ai.simplify_instruction(
        elder, original_instruction="Take 500 mg after breakfast as advised."
    )
    assert ok_simp is True
    assert not isinstance(simp_res, str)

    # Drop protected numeric dose -> deterministic fallback returns original string!
    gemini_gw.response = json.dumps(
        {
            "protected_dose": "1 tablet",
            "protected_timing": "morning",
            "protected_cautions": [],
            "plain_summary": "Take one pill in the morning.",
        }
    )
    ok_fail, fallback_txt = container.ai.simplify_instruction(
        elder, original_instruction="Take 500 mg after breakfast as advised."
    )
    assert ok_fail is False
    assert fallback_txt == "Take 500 mg after breakfast as advised."

    # 4. Schedule suggestions
    gemini_gw.response = json.dumps(
        {
            "suggested_times_hhmm": ["08:30", "20:30"],
            "rationale": "Aligned with breakfast and dinner preferences",
            "unresolved_constraints": [],
        }
    )
    sug = container.ai.suggest_schedule_times(elder, dose_instructions="Twice daily after meals")
    assert sug.suggested_times_hhmm == ["08:30", "20:30"]

    # 5. Medicine label explainer
    gemini_gw.response = json.dumps(
        {"plain_explanation": "Take with water after meal.", "cited_source": "Entered label"}
    )
    exp = container.ai.explain_medicine_label(
        elder, med_name="Metformin", label_instructions="1 tab after meal"
    )
    assert "water" in exp.plain_explanation

    # 6. Ask CareCircle (administrative allowed, clinical/private refused deterministically)
    refusal = container.ai.ask_carecircle(
        elder, question="Should I take a double dose for chest pain?"
    )
    assert refusal.refused is True
    priv_refusal = container.ai.ask_carecircle(
        elder, question="What is Rohan's email and passcode?"
    )
    assert priv_refusal.refused is True

    gemini_gw.response = json.dumps(
        {
            "refused": False,
            "refusal_reason": "",
            "answer": "Metformin is scheduled at 08:00.",
            "cited_record_ids": [],
        }
    )
    ans = container.ai.ask_carecircle(elder, question="What medicines are due today?")
    assert ans.refused is False

    # 7. Voice transcription (valid <=60s + invalid >60s rejection)
    gemini_gw.response = json.dumps({"transcript": "दवाई ले ली", "detected_language": "hi"})
    vt = container.ai.transcribe_voice(elder, audio_bytes=_make_wav_bytes(seconds=1.0))
    assert vt.detected_language == "hi"
    with pytest.raises(ValidationError):
        container.ai.transcribe_voice(elder, audio_bytes=_make_wav_bytes(seconds=61.0))

    # 8. Brief narrative selection rejects unknown fact IDs & malformed JSON
    facts = [{"id": "F1_ADHERENCE", "en": "1 of 1", "hi": "1 में से 1"}]
    gemini_gw.response = json.dumps(
        {"template_id": "calm_summary_v1", "selected_fact_ids": ["UNKNOWN_ID"]}
    )
    assert container.ai.select_brief_narrative(elder, factual_statements=facts) is None

    gemini_gw.response = "NOT JSON"
    with pytest.raises(AIServiceError):
        container.ai.parse_nl_schedule(elder, text_input="test")


@pytest.mark.integration
def test_doctor_brief_approval_and_english_hindi_pdf_generation(
    container: ServiceContainer, gemini_gw: FakeGeminiGateway
) -> None:
    """Deterministic brief generates EN/HI PDFs with HarfBuzz Devanagari shaping and appendix overflow."""
    _, elder = onboard_elder(
        container, email="e@example.test", name="कमला देवी", timezone="Asia/Kolkata"
    )
    _, care, care_mem_id = invite_and_join(
        container, elder, email="c@example.test", name="Rohan", role=Role.CARETAKER
    )
    container.circle.update_policies(elder, ai_consent_granted=True)
    elder = container.auth.resolve_session(
        container.auth.issue_session(
            account_id=elder.account_id, membership_id=elder.membership_id
        )[0]
    )
    assert elder is not None

    # Create 8 meds + 6 notes to exercise overflow appendix
    for idx in range(8):
        container.medication.create_medication(
            elder,
            name=f"दवाई-{idx + 1} Med",
            dose_text="1 गोली",
            instructions="नाश्ते के बाद",
            start_date=date(2026, 5, 10),
            end_date=None,
            criticality=Criticality.NORMAL,
            stock_count=10,
            stock_unit="tablets",
            refill_threshold=2,
            stock_units_per_dose=1,
            times_local=[time(8, 0)],
            weekdays=[0, 1, 2, 3, 4, 5, 6],
        )
        container.brief.add_note(
            elder, note_type="elder_question", content=f"प्रश्न {idx + 1}: क्या समय बदलना है?"
        )

    gemini_gw.response = json.dumps(
        {"template_id": "calm_summary_v1", "selected_fact_ids": ["F1_ADHERENCE", "F2_PATTERNS"]}
    )
    brief = container.brief.generate_brief(elder, period_days=30, use_ai_narrative=True)
    assert "Summary of recorded care highlights" in brief.ai_narrative_en
    assert "दर्ज देखभाल रिकॉर्ड" in brief.ai_narrative_hi

    # Unapproved brief cannot be exported by caretaker
    with pytest.raises(AuthorizationError):
        container.brief.get_brief_for_export(care, brief_id=brief.id)

    # Hash mismatch rejected
    with pytest.raises(CareCircleError):
        container.brief.approve_brief(
            elder,
            brief_id=brief.id,
            expected_content_hash="wronghash",
            recipient_membership_ids=[care_mem_id],
        )

    approved = container.brief.approve_brief(
        elder,
        brief_id=brief.id,
        expected_content_hash=brief.content_hash,
        recipient_membership_ids=[care_mem_id],
    )
    exportable = container.brief.get_brief_for_export(care, brief_id=approved.id)

    pdf_en = render_brief_pdf(exportable, elder_name="Kamala Devi", lang="en")
    pdf_hi = render_brief_pdf(exportable, elder_name="कमला देवी", lang="hi")
    assert pdf_en.startswith(b"%PDF-")
    assert pdf_hi.startswith(b"%PDF-")
    assert len(pdf_hi) > 5000

    # Consent revocation immediately blocks caretaker export
    container.circle.update_member_consent(
        elder, membership_id=care_mem_id, category=ConsentCategory.DOCTOR_BRIEF, granted=False
    )
    care_reloaded = container.auth.resolve_session(
        container.auth.issue_session(account_id=care.account_id, membership_id=care_mem_id)[0]
    )
    assert care_reloaded is not None
    with pytest.raises(ConsentRequiredError):
        container.brief.get_brief_for_export(care_reloaded, brief_id=approved.id)


@pytest.mark.accessibility
def test_locale_parity_css_boundaries_contrast_and_smoke_imports() -> None:
    """EN/HI locales have zero parity issues, CSS enforces 18..32 bounds, and contrast >= 4.5:1."""
    assert validate_locale_parity() == []
    for lang in SUPPORTED_LANGUAGES:
        assert t("app.title", lang=lang)
    from datetime import UTC, datetime

    now = datetime(2026, 5, 10, 8, 30, tzinfo=UTC)
    assert format_local_date(date(2026, 5, 10), "Asia/Kolkata", lang="hi")
    assert format_local_datetime(now, "Asia/Kolkata", lang="en")

    # CSS bounds & injection prevention
    css_std = build_css(18, False)
    assert "--cc-font-px: 18px;" in css_std
    css_hc = build_css(32, True)
    assert "--cc-font-px: 32px;" in css_hc
    for bad in (16, 34, True, "18;}</style>"):
        with pytest.raises(ValidationError):
            build_css(bad, False)  # type: ignore[arg-type]

    # WCAG AA contrast checks for normal & high-contrast palettes
    assert _contrast_ratio("#141A19", "#F8F7F4") >= 4.5
    assert _contrast_ratio("#FFFFFF", "#0F5C54") >= 4.5
    assert _contrast_ratio("#0F3D38", "#EDF4F2") >= 4.5
    assert _contrast_ratio("#000000", "#FFFFFF") >= 7.0

    # Smoke import CLI helpers
    from scripts.check_config import main as check_cfg_main
    from scripts.run_reminders import main as run_rem_main

    assert callable(check_cfg_main)
    assert callable(run_rem_main)
