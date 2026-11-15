"""Tests for security primitives, fail-closed config, auth, lockouts, onboarding, and sessions."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from core.config import AppConfig
from core.errors import (
    AccountLockedError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    RateLimitError,
    TokenInvalidError,
    ValidationError,
)
from core.logging import get_logger, redact_text, sanitize_log_fields
from core.models import ActionTokenType, ConsentCategory, FamilyGrant, Role
from core.security import (
    MAX_PASSCODE_UTF8_BYTES,
    MIN_PASSCODE_CHARS,
    escape_html,
    hash_passcode,
    neutralize_csv_cell,
    sign_action_token,
    verify_action_token,
    verify_passcode,
)
from repositories.demo_bundle import create_demo_repositories
from repositories.demo_store import DemoStore
from services.container import ServiceContainer, build_container
from tests.conftest import FrozenClock, invite_and_join, make_test_config, onboard_elder


@pytest.mark.unit
def test_passcode_hashing_and_length_bounds() -> None:
    """Passcodes <6 chars or >72 UTF-8 bytes are rejected without silent truncation."""
    with pytest.raises(ValidationError):
        hash_passcode("a" * (MIN_PASSCODE_CHARS - 1), rounds=4)

    with pytest.raises(ValidationError):
        hash_passcode("x" * (MAX_PASSCODE_UTF8_BYTES + 1), rounds=4)

    # Multi-byte UTF-8 Hindi text exceeding 72 bytes even if <72 characters
    hindi_over = "क" * 25  # 75 bytes in UTF-8
    assert len(hindi_over.encode("utf-8")) == 75
    with pytest.raises(ValidationError):
        hash_passcode(hindi_over, rounds=4)

    hashed = hash_passcode("validpass123", rounds=12)
    assert verify_passcode("validpass123", hashed) is True
    assert verify_passcode("wrongpass123", hashed) is False
    assert verify_passcode("x" * 80, hashed) is False


@pytest.mark.unit
def test_hmac_token_rotation_and_tamper_rejection() -> None:
    """HMAC tokens validate under current or previous key and reject tampered/expired tokens."""
    k1 = "primary-key-at-least-thirty-two-bytes-long"
    k0 = "previous-key-at-least-thirty-two-bytes-long"
    now = FrozenClock().now()
    tok_v1, _ = sign_action_token(
        key_id="v1",
        signing_key=k0,
        action=ActionTokenType.DOSE_TAKEN,
        circle_id=uuid.uuid4(),
        resource_type="dose",
        resource_id=uuid.uuid4(),
        recipient_membership_id=uuid.uuid4(),
        expires_at=now + timedelta(hours=2),
    )
    verified = verify_action_token(
        tok_v1,
        now=now,
        active_key_id="v2",
        active_key=k1,
        previous_key_id="v1",
        previous_key=k0,
    )
    assert verified.kid == "v1"

    with pytest.raises(TokenInvalidError):
        verify_action_token(
            tok_v1,
            now=now,
            active_key_id="v2",
            active_key=k1,
            previous_key_id="",
            previous_key="",
        )
    with pytest.raises(TokenInvalidError):
        verify_action_token(
            tok_v1 + "tamper",
            now=now,
            active_key_id="v2",
            active_key=k1,
            previous_key_id="v1",
            previous_key=k0,
        )
    with pytest.raises(TokenInvalidError):
        verify_action_token(
            tok_v1,
            now=now + timedelta(hours=3),
            active_key_id="v2",
            active_key=k1,
            previous_key_id="v1",
            previous_key=k0,
        )


@pytest.mark.unit
def test_sanitization_escaping_and_logging() -> None:
    """HTML escaping, CSV neutralization, and log token redaction protect outputs."""
    assert escape_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert neutralize_csv_cell("=CMD('calc')") == "'=CMD('calc')"
    assert neutralize_csv_cell("+1+1") == "'+1+1"
    assert neutralize_csv_cell("-2") == "'-2"
    assert neutralize_csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert neutralize_csv_cell("normal text") == "normal text"

    redacted = redact_text("https://app.test/?action_token=secret123&verify=abc")
    assert "secret123" not in redacted
    assert "abc" not in redacted
    assert "action_token=[REDACTED]" in redacted
    fields = sanitize_log_fields({"email": "a@b.test", "url": "https://x/?token=tok99"})
    assert fields["email"] == "[REDACTED]"
    assert "tok99" not in str(fields["url"])
    log = get_logger("test.carecircle")
    log.info("test_event", url="https://app.test/?token=secret999")


@pytest.mark.unit
def test_config_fail_closed_and_demo_seed_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production mode fails closed without Supabase + strong key; demo seed CLI refuses prod."""
    prod_cfg = AppConfig(demo_mode=False)
    with pytest.raises(ConfigurationError):
        prod_cfg.require_production_ready()
    with pytest.raises(ConfigurationError):
        build_container(prod_cfg)

    monkeypatch.setenv("CARECIRCLE_DEMO_MODE", "false")
    from scripts.seed_demo import main as seed_main

    assert seed_main(["--confirm-demo-seed"]) == 2


@pytest.mark.integration
def test_verified_elder_onboarding_and_second_circle(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Pending verification cannot sign in; preview is harmless; confirm creates active circle."""
    raw_tok = container.auth.start_elder_onboarding(
        email="Elder@Example.COM ",
        display_name="Asha",
        passcode="elder-pass-123",
        timezone="Asia/Kolkata",
        language="en",
    )
    # Harmless preview does not consume token or activate account
    preview = container.auth.preview_verification(raw_tok)
    assert preview["display_name"] == "Asha"

    with pytest.raises(AuthenticationError):
        container.auth.authenticate(email="elder@example.com", passcode="elder-pass-123")

    # Confirm consumes token once
    container.auth.confirm_verification(raw_tok)
    with pytest.raises(TokenInvalidError):
        container.auth.confirm_verification(raw_tok)

    # Duplicate registration fails safely at redemption without email enumeration on start
    dup_tok = container.auth.start_elder_onboarding(
        email="elder@example.com",
        display_name="Asha 2",
        passcode="elder-pass-123",
        timezone="Asia/Kolkata",
    )
    with pytest.raises(ConflictError):
        container.auth.confirm_verification(dup_tok)

    # Elder already owns one active circle (uq_one_active_owned_circle_per_elder); a non-owner member can create their own
    acc, mems = container.auth.authenticate(email="elder@example.com", passcode="elder-pass-123")
    assert len(mems) == 1
    _, actor1 = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    with pytest.raises(ConflictError):
        container.auth.create_own_circle(
            actor1, display_name="Duplicate Owned Circle", timezone="America/New_York"
        )
    _, care_actor, _ = invite_and_join(
        container, actor1, email="rohan-second@example.test", name="Rohan", role=Role.CARETAKER
    )
    new_circle, _ = container.auth.create_own_circle(
        care_actor, display_name="Rohan Own Circle", timezone="America/New_York"
    )
    assert new_circle.timezone == "America/New_York"
    _, mems2 = container.auth.authenticate(
        email="rohan-second@example.test", passcode="member-pass-123"
    )
    assert len(mems2) == 2

    # Expired verification token rejected
    tok2 = container.auth.start_elder_onboarding(
        email="late@example.test",
        display_name="Late",
        passcode="elder-pass-123",
        timezone="UTC",
    )
    clock.advance(hours=25)
    with pytest.raises(TokenInvalidError):
        container.auth.confirm_verification(tok2)


@pytest.mark.integration
def test_login_lockout_concurrency_and_rate_limit(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """5 failed logins lock account for 15 minutes atomically; unknown email gets generic error."""
    onboard_elder(container, email="asha@example.test", passcode="correct-pass-1")

    with pytest.raises(AuthenticationError):
        container.auth.authenticate(email="nobody@example.test", passcode="wrong-pass-1")

    def attempt_bad(_: int) -> str:
        try:
            container.auth.authenticate(email="asha@example.test", passcode="wrong-pass-1")
            return "ok"
        except AccountLockedError:
            return "locked"
        except AuthenticationError:
            return "failed"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt_bad, range(7)))
    assert results.count("failed") == 4
    assert results.count("locked") == 3

    # Even right passcode rejected while locked
    with pytest.raises(AccountLockedError):
        container.auth.authenticate(email="asha@example.test", passcode="correct-pass-1")

    # After 15 minutes lockout clears
    clock.advance(minutes=16)
    acc, _ = container.auth.authenticate(email="asha@example.test", passcode="correct-pass-1")
    assert acc.failed_login_attempts == 0

    # Global per-minute rate limit enforced
    cfg_tight = make_test_config(login_global_rate_limit_per_min=3)
    store2 = DemoStore()
    _, repos2 = create_demo_repositories(store2)
    c2 = build_container(cfg_tight, clock=clock, repos=repos2, seed_demo=False)
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            c2.auth.authenticate(email="x@y.test", passcode="wrong-pass-1")
    with pytest.raises(RateLimitError):
        c2.auth.authenticate(email="x@y.test", passcode="wrong-pass-1")


@pytest.mark.integration
def test_invitation_email_binding_and_existing_account_preservation(
    container: ServiceContainer,
) -> None:
    """Invites bind to invited email; existing accounts keep original passcode across circles."""
    _, elder1 = onboard_elder(container, email="e1@example.test", name="Elder One")
    _, elder2 = onboard_elder(container, email="e2@example.test", name="Elder Two")

    # Caretaker joins circle 1 with passcode A
    _, care_actor, _ = invite_and_join(
        container,
        elder1,
        email="rohan@example.test",
        name="Rohan",
        role=Role.CARETAKER,
        passcode="rohan-original-pass",
    )
    assert care_actor.role == Role.CARETAKER

    # Invite same existing account to circle 2
    inv2 = container.circle.create_invitation(
        elder2,
        invited_email="rohan@example.test",
        role=Role.FAMILY,
        grants=frozenset({FamilyGrant.ADD_OWN_NOTE}),
        initial_consents=frozenset({ConsentCategory.DOCTOR_BRIEF}),
        is_care_contact=False,
    )
    prev = container.circle.preview_invitation(inv2)
    assert prev["existing_account"] is True

    # Wrong passcode for existing account fails
    with pytest.raises(AuthenticationError):
        container.circle.accept_invitation(raw_token=inv2, passcode="different-pass")

    # Correct passcode joins circle 2 without overwriting passcode
    container.circle.accept_invitation(raw_token=inv2, passcode="rohan-original-pass")
    _, mems = container.auth.authenticate(
        email="rohan@example.test", passcode="rohan-original-pass"
    )
    assert len(mems) == 2


@pytest.mark.integration
def test_session_idle_absolute_expiry_logout_and_passcode_revocation(
    container: ServiceContainer, clock: FrozenClock
) -> None:
    """Sessions expire after 30m idle or 12h absolute, and revoke on logout or passcode change."""
    sess_tok, _ = onboard_elder(container, email="sess@example.test", passcode="elder-pass-123")
    assert container.auth.resolve_session(sess_tok) is not None

    # Activity at 20m extends idle window
    clock.advance(minutes=20)
    assert container.auth.resolve_session(sess_tok) is not None
    # 31m without activity expires idle window
    clock.advance(minutes=31)
    assert container.auth.resolve_session(sess_tok) is None

    # New session hits 12h absolute ceiling even with periodic activity
    acc, mems = container.auth.authenticate(email="sess@example.test", passcode="elder-pass-123")
    sess2, _ = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    resolved = None
    for _ in range(30):
        clock.advance(minutes=25)
        resolved = container.auth.resolve_session(sess2)
    assert resolved is None

    # Logout revokes session
    sess3, _ = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    container.auth.logout(sess3)
    assert container.auth.resolve_session(sess3) is None

    # Passcode change revokes all sessions
    sess4, actor4 = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    container.auth.change_own_passcode(
        actor4, current_passcode="elder-pass-123", new_passcode="new-pass-456"
    )
    assert container.auth.resolve_session(sess4) is None
    container.auth.authenticate(email="sess@example.test", passcode="new-pass-456")

    # Update profile & preferences + telegram
    sess5, actor5 = container.auth.issue_session(account_id=acc.id, membership_id=mems[0][0].id)
    updated_acc = container.auth.update_own_preferences(
        actor5,
        language="hi",
        font_px=28,
        high_contrast=True,
    )
    updated_acc = container.auth.update_own_telegram(actor5, opt_in=True, chat_id="12345")
    assert updated_acc.language == "hi"
    assert updated_acc.font_px == 28
    assert updated_acc.high_contrast is True
    assert updated_acc.telegram_opt_in is True
    assert container.auth.resolve_session(sess5) is not None
