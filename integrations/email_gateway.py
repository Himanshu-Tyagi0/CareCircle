"""Email delivery gateway with certificate-validated TLS, timeouts, and uncertainty tracking."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as StdEmailMessage
from enum import StrEnum
from typing import Protocol


class DeliveryState(StrEnum):
    """Explicit outcome classification for provider handoff."""

    ACCEPTED = "accepted"
    DEFINITIVE_FAILURE = "definitive_failure"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class EmailPayload:
    """Outbound minimal email message constructed immediately before send."""

    to_address: str
    subject: str
    text_body: str
    html_body: str
    message_id: str


@dataclass(frozen=True)
class DeliveryResult:
    """Result returned by email/Telegram gateways."""

    state: DeliveryState
    provider_message_id: str = ""
    error_code: str = ""


class EmailGateway(Protocol):
    """Protocol for replaceable email delivery gateways."""

    def send(self, payload: EmailPayload) -> DeliveryResult:
        """Attempt to deliver an email and classify accepted/definitive_failure/uncertain."""
        ...


class SmtpEmailGateway:
    """Gmail-compatible SMTP gateway using certificate-validated TLS and explicit timeouts."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        app_password: str,
        from_address: str,
        from_name: str = "CareCircle",
        use_ssl: bool = False,
        timeout_seconds: int = 15,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._app_password = app_password
        self._from_address = from_address
        self._from_name = from_name
        self._use_ssl = use_ssl
        self._timeout = timeout_seconds

    def send(self, payload: EmailPayload) -> DeliveryResult:
        """Send an email over TLS-verified SMTP with deterministic pre/post-accept handling."""
        msg = StdEmailMessage()
        msg["Subject"] = payload.subject
        msg["From"] = f"{self._from_name} <{self._from_address}>"
        msg["To"] = payload.to_address
        msg["Message-ID"] = payload.message_id
        msg.set_content(payload.text_body)
        msg.add_alternative(payload.html_body, subtype="html")

        ctx = ssl.create_default_context()
        data_entered = False
        try:
            if self._use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._host, self._port, timeout=self._timeout, context=ctx
                )
            else:
                server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            with server:
                server.ehlo()
                if not self._use_ssl:
                    server.starttls(context=ctx)
                    server.ehlo()
                server.login(self._username, self._app_password)
                data_entered = True
                refused = server.send_message(msg)
                if refused:
                    return DeliveryResult(
                        state=DeliveryState.DEFINITIVE_FAILURE,
                        error_code="smtp_recipient_refused",
                    )
                return DeliveryResult(
                    state=DeliveryState.ACCEPTED,
                    provider_message_id=payload.message_id,
                )
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused):
            return DeliveryResult(
                state=DeliveryState.DEFINITIVE_FAILURE,
                error_code="smtp_pre_accept_rejected",
            )
        except (TimeoutError, smtplib.SMTPServerDisconnected, OSError):
            if data_entered:
                return DeliveryResult(
                    state=DeliveryState.UNCERTAIN,
                    provider_message_id=payload.message_id,
                    error_code="smtp_ambiguous_timeout",
                )
            return DeliveryResult(
                state=DeliveryState.DEFINITIVE_FAILURE,
                error_code="smtp_connect_failed",
            )
        except smtplib.SMTPException:
            return DeliveryResult(
                state=DeliveryState.UNCERTAIN if data_entered else DeliveryState.DEFINITIVE_FAILURE,
                provider_message_id=payload.message_id if data_entered else "",
                error_code="smtp_protocol_error",
            )


@dataclass
class DemoEmailGateway:
    """Isolated in-memory email gateway that never sends real outbound emails."""

    sent_messages: list[EmailPayload] = field(default_factory=list)
    fail_mode: str = ""  # "", "definitive", "uncertain"

    def send(self, payload: EmailPayload) -> DeliveryResult:
        """Capture outbound email in memory or simulate failure modes for tests/demo."""
        if self.fail_mode == "definitive":
            return DeliveryResult(
                state=DeliveryState.DEFINITIVE_FAILURE, error_code="demo_simulated_failure"
            )
        if self.fail_mode == "uncertain":
            return DeliveryResult(
                state=DeliveryState.UNCERTAIN,
                provider_message_id=payload.message_id,
                error_code="demo_simulated_timeout",
            )
        self.sent_messages.append(payload)
        return DeliveryResult(
            state=DeliveryState.ACCEPTED,
            provider_message_id=payload.message_id,
        )
