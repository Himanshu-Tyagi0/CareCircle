"""Bounded AI output schemas, media sanitizers, and deterministic preservation validators."""

from __future__ import annotations

import io
import re
import wave
from typing import Literal

from core.errors import AIServiceError, ValidationError
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 60.0
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}


class PrescriptionMedDraft(BaseModel):
    """Unverified draft extracted from a prescription image."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(max_length=120)
    strength: str = Field(default="", max_length=60)
    dose_instructions: str = Field(default="", max_length=250)
    frequency: str = Field(default="", max_length=80)
    duration: str = Field(default="", max_length=80)
    uncertain_fields: list[str] = Field(default_factory=list, max_length=8)


class PrescriptionScanResult(BaseModel):
    """Bounded list of extracted medicine drafts."""

    model_config = ConfigDict(frozen=True)

    items: list[PrescriptionMedDraft] = Field(default_factory=list, max_length=20)
    needs_human_review: bool = True


class NLScheduleDraft(BaseModel):
    """Parsed natural-language schedule draft requiring human confirmation."""

    model_config = ConfigDict(frozen=True)

    medication_name: str = Field(default="", max_length=120)
    dose_text: str = Field(default="", max_length=120)
    times_hhmm: list[str] = Field(default_factory=list, max_length=8)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=6)


class SimplifiedInstructionResult(BaseModel):
    """Constrained template output preserving protected factual numbers/cautions."""

    model_config = ConfigDict(frozen=True)

    protected_dose: str = Field(max_length=120)
    protected_timing: str = Field(max_length=120)
    protected_cautions: list[str] = Field(default_factory=list, max_length=6)
    plain_summary: str = Field(max_length=300)


class ScheduleSuggestionResult(BaseModel):
    """Organizational reminder-time suggestions within existing prescription bounds."""

    model_config = ConfigDict(frozen=True)

    suggested_times_hhmm: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(default="", max_length=300)
    unresolved_constraints: list[str] = Field(default_factory=list, max_length=6)


class MedicineExplanationResult(BaseModel):
    """Plain-language explanation grounded strictly in provided label/source text."""

    model_config = ConfigDict(frozen=True)

    plain_explanation: str = Field(max_length=500)
    cited_source: str = Field(max_length=200)


class AskCareCircleResult(BaseModel):
    """Bounded administrative answer with cited record references or safe refusal."""

    model_config = ConfigDict(frozen=True)

    refused: bool = False
    refusal_reason: str = Field(default="", max_length=120)
    answer: str = Field(max_length=500)
    cited_record_ids: list[str] = Field(default_factory=list, max_length=10)


class VoiceTranscriptResult(BaseModel):
    """Transcribed user audio awaiting user review and editing before action."""

    model_config = ConfigDict(frozen=True)

    transcript: str = Field(max_length=600)
    detected_language: Literal["en", "hi", "unknown"] = "en"


class BriefNarrativeSelection(BaseModel):
    """Constrained AI selection of existing factual statement IDs + allowlisted template."""

    model_config = ConfigDict(frozen=True)

    template_id: Literal["calm_summary_v1", "adherence_focus_v1"] = "calm_summary_v1"
    selected_fact_ids: list[str] = Field(default_factory=list, max_length=12)


_NUMBER_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|tablet|tablets|pill|pills|drop|drops|hr|hrs|hour|hours)?\b",
    re.I,
)


def sanitize_uploaded_image(raw_bytes: bytes) -> tuple[bytes, str]:
    """Validate image MIME/decoding/size/pixels and strip EXIF/metadata."""
    if not raw_bytes or len(raw_bytes) > MAX_IMAGE_BYTES:
        raise ValidationError("errors.validation")
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ALLOWED_IMAGE_FORMATS:
                raise ValidationError("errors.validation")
            w, h = img.size
            if w <= 0 or h <= 0 or (w * h) > MAX_IMAGE_PIXELS:
                raise ValidationError("errors.validation")
            clean = Image.new("RGB" if fmt == "JPEG" else img.mode, img.size)
            clean.paste(img)
            buf = io.BytesIO()
            out_fmt = "PNG" if fmt != "JPEG" else "JPEG"
            clean.save(buf, format=out_fmt)
            mime = "image/jpeg" if out_fmt == "JPEG" else "image/png"
            return buf.getvalue(), mime
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("errors.validation") from exc


def validate_uploaded_audio(raw_bytes: bytes) -> tuple[bytes, str, float]:
    """Validate WAV/audio size (<=10MB) and actual duration (<=60s)."""
    if not raw_bytes or len(raw_bytes) > MAX_AUDIO_BYTES:
        raise ValidationError("errors.validation")
    try:
        with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                raise ValidationError("errors.validation")
            duration = frames / float(rate)
            if duration <= 0 or duration > MAX_AUDIO_SECONDS:
                raise ValidationError("errors.validation")
            return raw_bytes, "audio/wav", duration
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("errors.validation") from exc


def verify_factual_preservation(
    original_text: str, simplified: SimplifiedInstructionResult
) -> bool:
    """Verify all numeric quantities/frequencies in original remain intact and 'as advised' isn't rewritten."""
    orig_lower = original_text.lower()
    summ_lower = simplified.plain_summary.lower()
    if "as advised" in orig_lower and "as advised" not in summ_lower and "doctor" not in summ_lower:
        return False
    orig_nums = {m.strip().lower() for m in _NUMBER_UNIT_RE.findall(original_text) if m.strip()}
    combined_out = f"{simplified.protected_dose} {simplified.protected_timing} {simplified.plain_summary}".lower()
    return all(token in combined_out for token in orig_nums)


def parse_bounded_json(raw_text: str, model_cls: type[BaseModel]) -> BaseModel:
    """Validate untrusted model JSON strictly against a Pydantic schema with size limits."""
    if not raw_text or len(raw_text) > 12_000:
        raise AIServiceError("errors.ai_unavailable")
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    try:
        return model_cls.model_validate_json(cleaned)
    except PydanticValidationError as exc:
        raise AIServiceError("errors.ai_unavailable") from exc
