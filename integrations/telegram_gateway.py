"""Optional Telegram Bot API delivery gateway with explicit timeout and uncertainty handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from integrations.email_gateway import DeliveryResult, DeliveryState


@dataclass(frozen=True)
class TelegramPayload:
    """Minimal localized Telegram message."""

    chat_id: str
    text: str
    client_ref: str


class TelegramGateway(Protocol):
    """Protocol for Telegram notification delivery."""

    def send(self, payload: TelegramPayload) -> DeliveryResult:
        """Send a Telegram message and return explicit delivery state."""
        ...


class HttpTelegramGateway:
    """Telegram Bot API client with explicit timeouts."""

    def __init__(self, *, bot_token: str, timeout_seconds: int = 10) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._timeout = timeout_seconds

    def send(self, payload: TelegramPayload) -> DeliveryResult:
        """Deliver a Telegram message safely without leaking tokens."""
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": payload.chat_id,
                        "text": payload.text,
                        "disable_web_page_preview": True,
                    },
                )
            if 200 <= resp.status_code < 300:
                data = resp.json()
                msg_id = str(data.get("result", {}).get("message_id", payload.client_ref))
                return DeliveryResult(state=DeliveryState.ACCEPTED, provider_message_id=msg_id)
            if 400 <= resp.status_code < 500:
                return DeliveryResult(
                    state=DeliveryState.DEFINITIVE_FAILURE,
                    error_code=f"telegram_http_{resp.status_code}",
                )
            return DeliveryResult(
                state=DeliveryState.UNCERTAIN,
                error_code=f"telegram_http_{resp.status_code}",
            )
        except httpx.ConnectError:
            return DeliveryResult(
                state=DeliveryState.DEFINITIVE_FAILURE,
                error_code="telegram_connect_error",
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return DeliveryResult(
                state=DeliveryState.UNCERTAIN,
                error_code="telegram_ambiguous_timeout",
            )


@dataclass
class DemoTelegramGateway:
    """Isolated Telegram gateway capturing messages in memory only."""

    sent_messages: list[TelegramPayload] = field(default_factory=list)

    def send(self, payload: TelegramPayload) -> DeliveryResult:
        """Store Telegram message in memory without network I/O."""
        self.sent_messages.append(payload)
        return DeliveryResult(
            state=DeliveryState.ACCEPTED,
            provider_message_id=f"demo-tg-{payload.client_ref}",
        )
