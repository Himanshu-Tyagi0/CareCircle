"""Optional Google Gemini gateway using google-genai with strict timeouts and fallbacks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.errors import AIServiceError


class GeminiGateway(Protocol):
    """Protocol for bounded Gemini model invocation."""

    @property
    def enabled(self) -> bool:
        """Whether a live or test Gemini provider is configured."""
        ...

    def generate_json(
        self,
        *,
        system_instruction: str,
        prompt: str,
        image_bytes: bytes | None = None,
        image_mime: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
        max_output_tokens: int = 1024,
        timeout_seconds: int = 20,
    ) -> str:
        """Invoke model and return raw JSON/text string within limits."""
        ...


class DisabledGeminiGateway:
    """Explicitly disabled gateway used when GEMINI_API_KEY is absent or in demo mode."""

    @property
    def enabled(self) -> bool:
        """Always False when AI is disabled."""
        return False

    def generate_json(
        self,
        *,
        system_instruction: str,
        prompt: str,
        image_bytes: bytes | None = None,
        image_mime: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
        max_output_tokens: int = 1024,
        timeout_seconds: int = 20,
    ) -> str:
        """Raise AIServiceError cleanly."""
        raise AIServiceError("errors.ai_unavailable")


class GoogleGenAIGateway:
    """Production Gemini gateway built on google-genai SDK."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @property
    def enabled(self) -> bool:
        """True when API key and model are configured."""
        return bool(self._api_key and self._model)

    def generate_json(
        self,
        *,
        system_instruction: str,
        prompt: str,
        image_bytes: bytes | None = None,
        image_mime: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
        max_output_tokens: int = 1024,
        timeout_seconds: int = 20,
    ) -> str:
        """Call Google Gemini with strict output token and timeout limits."""
        if not self.enabled:
            raise AIServiceError("errors.ai_unavailable")
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
            )
            contents: list[object] = [prompt]
            if image_bytes and image_mime:
                contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))
            if audio_bytes and audio_mime:
                contents.append(types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime))

            response = client.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=max_output_tokens,
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            text = (response.text or "").strip()
            if not text:
                raise AIServiceError("errors.ai_unavailable")
            return text
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError("errors.ai_unavailable") from exc


@dataclass
class MockGeminiGateway:
    """Deterministic mock Gemini gateway for unit tests."""

    responses: list[str] = field(default_factory=list)
    enabled_flag: bool = True
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        """Return configured test enablement flag."""
        return self.enabled_flag

    def generate_json(
        self,
        *,
        system_instruction: str,
        prompt: str,
        image_bytes: bytes | None = None,
        image_mime: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
        max_output_tokens: int = 1024,
        timeout_seconds: int = 20,
    ) -> str:
        """Record call metadata and return next deterministic response."""
        self.calls.append(
            {
                "prompt": prompt,
                "has_image": image_bytes is not None,
                "has_audio": audio_bytes is not None,
            }
        )
        if not self.enabled_flag or not self.responses:
            raise AIServiceError("errors.ai_unavailable")
        return self.responses.pop(0)
