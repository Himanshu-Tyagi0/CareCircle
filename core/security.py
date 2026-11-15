"""Cryptographic, passcode, opaque token, HMAC action-token, and escaping utilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

import bcrypt
from pydantic import BaseModel, ConfigDict

from core.errors import TokenInvalidError, ValidationError
from core.models import ActionTokenType

MIN_PASSCODE_CHARS = 6
MAX_PASSCODE_UTF8_BYTES = 72
_EMAIL_BASIC_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"carecircle-dummy-passcode", bcrypt.gensalt(rounds=12)).decode(
    "ascii"
)


class SignedActionPayload(BaseModel):
    """Decoded and signature-verified signed action-token payload."""

    model_config = ConfigDict(frozen=True)

    v: int = 1
    kid: str
    nonce: str
    act: ActionTokenType
    cid: UUID
    rt: str
    rid: UUID
    mid: UUID
    exp: int


def normalize_email(raw_email: str) -> str:
    """Normalize and validate an email address consistently."""
    cleaned = raw_email.strip().lower()
    if not cleaned or len(cleaned) > 254 or not _EMAIL_BASIC_RE.match(cleaned):
        raise ValidationError("errors.invalid_email")
    return cleaned


def validate_passcode(raw_passcode: str) -> bytes:
    """Validate >=6 chars and <=72 UTF-8 bytes without silent truncation."""
    if len(raw_passcode) < MIN_PASSCODE_CHARS:
        raise ValidationError("errors.passcode_too_short", params={"min_chars": MIN_PASSCODE_CHARS})
    encoded = raw_passcode.encode("utf-8")
    if len(encoded) > MAX_PASSCODE_UTF8_BYTES:
        raise ValidationError(
            "errors.passcode_too_long", params={"max_bytes": MAX_PASSCODE_UTF8_BYTES}
        )
    return encoded


def hash_passcode(raw_passcode: str, *, rounds: int = 12) -> str:
    """Hash a validated passcode with bcrypt at the configured work factor."""
    encoded = validate_passcode(raw_passcode)
    work_factor = max(rounds, 4)
    hashed = bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=work_factor))
    return hashed.decode("ascii")


def verify_passcode(raw_passcode: str, passcode_hash: str) -> bool:
    """Verify a passcode against a bcrypt hash without silently truncating >72 bytes."""
    encoded = raw_passcode.encode("utf-8")
    if len(raw_passcode) < MIN_PASSCODE_CHARS or len(encoded) > MAX_PASSCODE_UTF8_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, passcode_hash.encode("ascii"))
    except ValueError:
        return False


def dummy_verify_passcode(raw_passcode: str) -> None:
    """Perform constant-cost dummy bcrypt work when an email is unknown."""
    encoded = raw_passcode.encode("utf-8")[:MAX_PASSCODE_UTF8_BYTES] or b"x"
    bcrypt.checkpw(encoded, _DUMMY_BCRYPT_HASH.encode("ascii"))


def generate_opaque_token(nbytes: int = 32) -> str:
    """Generate a URL-safe cryptographically random opaque token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 digest of a raw token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_action_token(
    *,
    key_id: str,
    signing_key: str,
    action: ActionTokenType,
    circle_id: UUID,
    resource_type: str,
    resource_id: UUID,
    recipient_membership_id: UUID,
    expires_at: datetime,
) -> tuple[str, str]:
    """Create an HMAC-SHA256 signed token with opaque IDs and return (raw_token, token_hash)."""
    if len(signing_key.encode("utf-8")) < 16:
        raise ValidationError("errors.config_missing", params={"missing": "ACTION_SIGNING_KEY"})
    payload = {
        "v": 1,
        "kid": key_id,
        "nonce": secrets.token_urlsafe(16),
        "act": action.value,
        "cid": str(circle_id),
        "rt": resource_type,
        "rid": str(resource_id),
        "mid": str(recipient_membership_id),
        "exp": int(expires_at.astimezone(UTC).timestamp()),
    }
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = hmac.new(
        signing_key.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    raw_token = f"{encoded_payload}.{_b64url_encode(sig)}"
    return raw_token, hash_token(raw_token)


def verify_action_token(
    raw_token: str,
    *,
    now: datetime,
    active_key_id: str,
    active_key: str,
    previous_key_id: str = "",
    previous_key: str = "",
) -> SignedActionPayload:
    """Verify HMAC signature (with optional key rotation), payload schema, and expiry."""
    if not raw_token or "." not in raw_token or len(raw_token) > 2048:
        raise TokenInvalidError("errors.token_invalid")

    parts = raw_token.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise TokenInvalidError("errors.token_invalid")
    encoded_payload, encoded_sig = parts

    try:
        payload_bytes = _b64url_decode(encoded_payload)
        payload = SignedActionPayload.model_validate_json(payload_bytes)
        provided_sig = _b64url_decode(encoded_sig)
    except Exception as exc:
        raise TokenInvalidError("errors.token_invalid") from exc

    key_for_kid: str | None = None
    if payload.kid == active_key_id and active_key:
        key_for_kid = active_key
    elif previous_key_id and payload.kid == previous_key_id and previous_key:
        key_for_kid = previous_key

    if key_for_kid is None:
        raise TokenInvalidError("errors.token_invalid")

    expected_sig = hmac.new(
        key_for_kid.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise TokenInvalidError("errors.token_invalid")

    if int(now.astimezone(UTC).timestamp()) >= payload.exp:
        raise TokenInvalidError("errors.token_expired")

    return payload


def escape_html(text: str) -> str:
    """Escape untrusted text safely for HTML email/UI contexts."""
    return html.escape(text, quote=True)


def neutralize_csv_cell(value: object) -> str:
    """Neutralize spreadsheet formula triggers (=, +, -, @, tab, CR) on CSV export."""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text
