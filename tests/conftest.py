"""Shared pytest fixtures: frozen clock, test config, demo store, and gateway fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from core.clock import Clock
from core.config import AppConfig
from core.models import ActorContext, ConsentCategory, FamilyGrant, Role
from integrations.email_gateway import DeliveryResult, DeliveryState, EmailPayload
from integrations.telegram_gateway import TelegramPayload
from repositories.demo_bundle import create_demo_repositories
from repositories.demo_store import DemoStore
from services.container import ServiceContainer, build_container


class FrozenClock(Clock):
    """Deterministic mutable clock for unit and integration tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2026, 5, 10, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt.astimezone(UTC)

    def advance(self, **kwargs: Any) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now


@dataclass
class FakeEmailGateway:
    """Configurable email gateway recording sent messages and simulating provider states."""

    next_state: DeliveryState = DeliveryState.ACCEPTED
    error_code: str = ""
    sent: list[EmailPayload] = field(default_factory=list)

    def send(self, payload: EmailPayload) -> DeliveryResult:
        self.sent.append(payload)
        if self.next_state == DeliveryState.ACCEPTED:
            return DeliveryResult(
                state=DeliveryState.ACCEPTED,
                provider_message_id=f"fake-email-{len(self.sent)}",
            )
        return DeliveryResult(
            state=self.next_state,
            error_code=self.error_code or "simulated_failure",
        )


@dataclass
class FakeTelegramGateway:
    """Configurable Telegram gateway recording dispatches."""

    next_state: DeliveryState = DeliveryState.ACCEPTED
    sent: list[TelegramPayload] = field(default_factory=list)

    def send(self, payload: TelegramPayload) -> DeliveryResult:
        self.sent.append(payload)
        if self.next_state == DeliveryState.ACCEPTED:
            return DeliveryResult(
                state=DeliveryState.ACCEPTED,
                provider_message_id=f"fake-tg-{len(self.sent)}",
            )
        return DeliveryResult(state=self.next_state, error_code="simulated_tg_err")


class FakeGeminiGateway:
    """Configurable Gemini gateway returning queued raw strings or raising errors."""

    def __init__(self, *, enabled: bool = True, response: str = "{}") -> None:
        self._enabled = enabled
        self.response = response
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def generate_json(
        self,
        *,
        system_instruction: str,
        prompt: str,
        image_bytes: bytes | None = None,
        image_mime: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
        max_output_tokens: int = 1200,
    ) -> str:
        self.calls.append(
            {
                "system_instruction": system_instruction,
                "prompt": prompt,
                "image_bytes": image_bytes,
                "image_mime": image_mime,
                "audio_bytes": audio_bytes,
                "audio_mime": audio_mime,
                "max_output_tokens": max_output_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def make_test_config(**overrides: Any) -> AppConfig:
    """Build a test AppConfig with fast bcrypt rounds=4 and valid rotated HMAC keys."""
    defaults: dict[str, Any] = {
        "demo_mode": True,
        "app_url": "https://carecircle.example.test",
        "action_signing_key_id": "v2",
        "action_signing_key": "test-primary-hmac-key-with-at-least-32-chars",
        "action_signing_previous_key_id": "v1",
        "action_signing_previous_key": "test-previous-hmac-key-with-at-least-32-chars",
        "bcrypt_rounds": 4,
        "login_max_failures": 5,
        "login_lockout_minutes": 15,
        "login_global_rate_limit_per_min": 60,
        "session_idle_minutes": 30,
        "session_absolute_hours": 12,
        "adherence_grace_minutes": 30,
        "telegram_bot_token": "fake-tg-token-for-unit-tests",
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def email_gw() -> FakeEmailGateway:
    return FakeEmailGateway()


@pytest.fixture
def tg_gw() -> FakeTelegramGateway:
    return FakeTelegramGateway()


@pytest.fixture
def gemini_gw() -> FakeGeminiGateway:
    return FakeGeminiGateway()


@pytest.fixture
def container(
    clock: FrozenClock,
    email_gw: FakeEmailGateway,
    tg_gw: FakeTelegramGateway,
    gemini_gw: FakeGeminiGateway,
) -> ServiceContainer:
    cfg = make_test_config()
    store = DemoStore()
    _, repos = create_demo_repositories(store)
    return build_container(
        cfg,
        clock=clock,
        repos=repos,
        email_gateway=email_gw,
        telegram_gateway=tg_gw,
        gemini_gateway=gemini_gw,
        seed_demo=False,
    )


def onboard_elder(
    container: ServiceContainer,
    *,
    email: str = "asha@example.test",
    name: str = "Asha",
    passcode: str = "elder-pass-123",
    timezone: str = "Asia/Kolkata",
    language: str = "en",
) -> tuple[str, ActorContext]:
    """Onboard, verify, and sign in an elder returning (session_token, ActorContext)."""
    tok = container.auth.start_elder_onboarding(
        email=email,
        display_name=name,
        passcode=passcode,
        timezone=timezone,
        language=language,
    )
    container.auth.confirm_verification(tok)
    acc, mems = container.auth.authenticate(email=email, passcode=passcode)
    sess_tok, actor = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    return sess_tok, actor


def invite_and_join(
    container: ServiceContainer,
    elder_actor: ActorContext,
    *,
    email: str,
    name: str,
    role: Role,
    passcode: str = "member-pass-123",
    grants: frozenset[FamilyGrant] = frozenset(),
    consents: frozenset[ConsentCategory] = frozenset(ConsentCategory),
    is_care_contact: bool = True,
) -> tuple[str, ActorContext, UUID]:
    """Create invitation as elder, accept it, and sign in returning (session_tok, actor, membership_id)."""
    inv_tok = container.circle.create_invitation(
        elder_actor,
        invited_email=email,
        role=role,
        grants=grants,
        initial_consents=consents,
        is_care_contact=is_care_contact,
    )
    container.circle.accept_invitation(raw_token=inv_tok, passcode=passcode, display_name=name)
    acc, mems = container.auth.authenticate(email=email, passcode=passcode)
    target_mem = next(m for m, _ in mems if m.circle_id == elder_actor.circle_id)
    sess_tok, mem_actor = container.auth.issue_session(
        account_id=acc.id, membership_id=target_mem.id
    )
    return sess_tok, mem_actor, target_mem.id
