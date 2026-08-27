"""Cryptographic primitives: password hashing, JWTs, phone hashing, MFA.

Pure functions only — no database, no Redis, no FastAPI.  That keeps them unit
testable without a running stack, which is what Section 17 asks for.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.errors import AppError
from app.core.permissions import Role

TokenType = Literal["access", "refresh", "mfa_pending"]

# Argon2id with parameters sized for an API server: ~50ms per hash on a modern
# core.  Raise time_cost if your production box is faster.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------
# phone numbers — PII rule, Section 12
# --------------------------------------------------------------------------
def normalise_phone(phone: str) -> str:
    """Normalise to E.164-ish digits.  Bare 10-digit input is assumed Indian."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    if digits.startswith("0") and len(digits) == 11:
        digits = "91" + digits[1:]
    return "+" + digits


def hash_phone(phone: str) -> str:
    """Deterministic HMAC of a phone number.

    Deterministic so we can look a pilgrim up by their number without ever
    storing it; keyed so a stolen database cannot be brute-forced against the
    (tiny) space of Indian phone numbers.
    """
    normalised = normalise_phone(phone)
    return hmac.new(
        settings.phone_hash_secret.encode("utf-8"),
        normalised.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _contact_cipher() -> Fernet:
    key = settings.contact_encryption_key
    if not key:
        raise AppError(
            "SERVICE_UNAVAILABLE",
            message="Contact encryption key is not configured.",
            details={"setting": "CONTACT_ENCRYPTION_KEY"},
        )
    return Fernet(key.encode("utf-8"))


def encrypt_contact(value: str) -> str:
    """Encrypt a raw phone number for the short-lived contact table."""
    return _contact_cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_contact(token: str) -> str:
    try:
        return _contact_cipher().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - key rotation accident
        raise AppError("INTERNAL_ERROR", message="Stored contact could not be decrypted.") from exc


def mask_phone(phone: str) -> str:
    """`+919876543210` -> `+91 ***** 43210`, for operator screens and logs."""
    normalised = normalise_phone(phone)
    return f"{normalised[:3]} ***** {normalised[-5:]}" if len(normalised) > 8 else "*****"


# --------------------------------------------------------------------------
# JWTs
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    role: Role
    token_type: TokenType
    jti: str
    family: str | None
    issued_at: datetime
    expires_at: datetime
    mfa_verified: bool
    raw: dict[str, Any]


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    *,
    subject: str,
    role: Role | str,
    mfa_verified: bool = False,
    extra: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    issued = now_utc()
    expires = issued + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": str(role),
        "type": "access",
        "jti": uuid.uuid4().hex,
        "mfa": mfa_verified,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": "wariverse-core-api",
    }
    if extra:
        payload.update(extra)
    return _encode(payload), expires


def create_refresh_token(
    *,
    subject: str,
    role: Role | str,
    family: str | None = None,
) -> tuple[str, str, str, datetime]:
    """Return (token, jti, family, expires_at).

    `family` links every rotation of one login session.  Presenting a refresh
    token that is not the family's current one means a stolen token is in play,
    and the whole family is revoked — see `TokenStore.rotate_refresh`.
    """
    issued = now_utc()
    expires = issued + timedelta(days=settings.refresh_token_ttl_days)
    jti = uuid.uuid4().hex
    family_id = family or uuid.uuid4().hex
    payload = {
        "sub": subject,
        "role": str(role),
        "type": "refresh",
        "jti": jti,
        "fam": family_id,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": "wariverse-core-api",
    }
    return _encode(payload), jti, family_id, expires


def create_mfa_pending_token(*, subject: str, role: Role | str) -> str:
    """Short-lived token proving password step passed, MFA step pending."""
    issued = now_utc()
    payload = {
        "sub": subject,
        "role": str(role),
        "type": "mfa_pending",
        "jti": uuid.uuid4().hex,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(minutes=5)).timestamp()),
        "iss": "wariverse-core-api",
    }
    return _encode(payload)


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="wariverse-core-api",
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError("TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise AppError("TOKEN_INVALID") from exc

    token_type = payload.get("type")
    if expected_type and token_type != expected_type:
        raise AppError("TOKEN_INVALID", details={"expected": expected_type, "got": token_type})

    try:
        role = Role(payload.get("role", ""))
    except ValueError as exc:
        raise AppError("TOKEN_INVALID", details={"reason": "unknown role"}) from exc

    return TokenClaims(
        subject=str(payload["sub"]),
        role=role,
        token_type=token_type,
        jti=str(payload["jti"]),
        family=payload.get("fam"),
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        mfa_verified=bool(payload.get("mfa", False)),
        raw=payload,
    )


# --------------------------------------------------------------------------
# MFA (Section 12: Administrator and System Admin)
# --------------------------------------------------------------------------
def generate_mfa_secret() -> str:
    return pyotp.random_base32()


def mfa_provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name="WariVerse")


def verify_mfa_code(secret: str, code: str) -> bool:
    # valid_window=1 tolerates one 30s step of clock drift on an operator phone.
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def generate_numeric_code(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def urlsafe_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# capability links
# --------------------------------------------------------------------------
#: Domain separation.  A capability link must never be mistakable for an access
#: token even if one of them is ever fed to the wrong verifier.
_CARD_LINK_CONTEXT = b"wariverse-pass-card-v1"


def _card_link_key() -> bytes:
    return hashlib.blake2b(settings.jwt_secret.encode("utf-8"), salt=b"card-link", digest_size=32).digest()


def sign_card_link(subject: str, expires_at: datetime) -> str:
    """Sign a bearer-in-a-URL for one pass's no-JavaScript card page.

    A URL, not a header, because the page it opens has to work in a browser with
    JavaScript switched off or broken — and such a browser cannot attach an
    `Authorization` header to a link.  The trade is real: anyone holding the URL
    holds the pass.  It is accepted because the pass it exposes is single-scan
    and its rolling code is only good for sixty seconds, so a leaked link buys
    an attacker the same thing a photograph of the screen would, and no more.

    Scoped to one pass and stamped with an expiry that the signature covers, so
    neither can be edited by the holder.
    """
    stamp = str(int(expires_at.timestamp()))
    body = f"{subject}.{stamp}".encode()
    mac = hmac.new(_card_link_key(), _CARD_LINK_CONTEXT + body, hashlib.sha256).digest()
    tag = base64.urlsafe_b64encode(mac[:16]).decode("ascii").rstrip("=")
    return f"{stamp}.{tag}"


def verify_card_link(subject: str, token: str) -> None:
    """Raise unless `token` is this pass's own unexpired link."""
    stamp, _, tag = (token or "").partition(".")
    if not stamp or not tag:
        raise AppError("TOKEN_INVALID", details={"reason": "malformed card link"})

    expected = sign_card_link(subject, datetime.fromtimestamp(int(stamp), tz=UTC)) if stamp.isdigit() else ""
    if not expected or not hmac.compare_digest(expected, f"{stamp}.{tag}"):
        raise AppError("TOKEN_INVALID", details={"reason": "card link signature"})

    if datetime.fromtimestamp(int(stamp), tz=UTC) <= now_utc():
        raise AppError("TOKEN_EXPIRED", details={"reason": "card link expired"})
