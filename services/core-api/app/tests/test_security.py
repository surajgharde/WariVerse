"""Crypto primitives — pure unit tests, no infrastructure needed."""

from __future__ import annotations

import time
from datetime import timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.core.permissions import Role
from app.core.security import (
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    decode_token,
    generate_mfa_secret,
    hash_password,
    hash_phone,
    mask_phone,
    normalise_phone,
    now_utc,
    verify_mfa_code,
    verify_password,
)


# --- passwords -------------------------------------------------------------
def test_password_hash_verifies_and_is_salted() -> None:
    first = hash_password("correct-horse-battery-staple")
    second = hash_password("correct-horse-battery-staple")
    assert first != second  # per-hash salt
    assert verify_password("correct-horse-battery-staple", first)
    assert not verify_password("wrong-password", first)


def test_password_verify_handles_missing_and_corrupt_hashes() -> None:
    assert not verify_password("anything", None)
    assert not verify_password("anything", "not-an-argon2-hash")


def test_hash_uses_argon2id() -> None:
    # Section 12 names the algorithm explicitly.
    assert hash_password("x" * 12).startswith("$argon2id$")


# --- phone handling --------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("91-9876543210", "+919876543210"),
    ],
)
def test_phone_normalisation_collapses_indian_formats(raw: str, expected: str) -> None:
    assert normalise_phone(raw) == expected


def test_phone_hash_is_deterministic_across_formats() -> None:
    # A pilgrim who types their number differently must resolve to one account.
    assert hash_phone("9876543210") == hash_phone("+91 98765 43210")


def test_phone_hash_differs_between_numbers() -> None:
    assert hash_phone("9876543210") != hash_phone("9876543211")


def test_phone_hash_does_not_contain_the_number() -> None:
    digest = hash_phone("9876543210")
    assert "9876543210" not in digest
    assert len(digest) == 64


def test_mask_phone_hides_the_middle() -> None:
    masked = mask_phone("9876543210")
    assert masked == "+91 ***** 43210"
    assert "98765" not in masked.replace("43210", "")


# --- JWTs ------------------------------------------------------------------
def test_access_token_round_trip() -> None:
    token, expires = create_access_token(subject="user-1", role=Role.SECURITY_OFFICER)
    claims = decode_token(token, expected_type="access")
    assert claims.subject == "user-1"
    assert claims.role is Role.SECURITY_OFFICER
    assert claims.token_type == "access"
    assert 0 < (expires - now_utc()).total_seconds() <= settings.access_token_ttl_minutes * 60


def test_refresh_token_carries_a_family() -> None:
    token, jti, family, expires = create_refresh_token(subject="user-1", role=Role.PILGRIM)
    claims = decode_token(token, expected_type="refresh")
    assert claims.jti == jti
    assert claims.family == family
    assert (expires - now_utc()) > timedelta(days=settings.refresh_token_ttl_days - 1)


def test_refresh_token_reuses_family_when_rotating() -> None:
    _, _, family, _ = create_refresh_token(subject="user-1", role=Role.PILGRIM)
    rotated, _, same_family, _ = create_refresh_token(subject="user-1", role=Role.PILGRIM, family=family)
    assert same_family == family
    assert decode_token(rotated).family == family


def test_token_type_confusion_is_rejected() -> None:
    """A refresh token must never be accepted where an access token is expected."""
    refresh, *_ = create_refresh_token(subject="user-1", role=Role.ADMINISTRATOR)
    with pytest.raises(AppError) as exc:
        decode_token(refresh, expected_type="access")
    assert exc.value.code == "TOKEN_INVALID"


def test_mfa_pending_token_is_not_an_access_token() -> None:
    token = create_mfa_pending_token(subject="user-1", role=Role.ADMINISTRATOR)
    with pytest.raises(AppError):
        decode_token(token, expected_type="access")
    assert decode_token(token, expected_type="mfa_pending").subject == "user-1"


def test_expired_token_is_reported_as_expired_not_invalid() -> None:
    expired = jwt.encode(
        {
            "sub": "user-1",
            "role": Role.PILGRIM.value,
            "type": "access",
            "jti": "abc",
            "iat": int(time.time()) - 120,
            "exp": int(time.time()) - 60,
            "iss": "wariverse-core-api",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AppError) as exc:
        decode_token(expired)
    assert exc.value.code == "TOKEN_EXPIRED"


def test_token_signed_with_another_key_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": "user-1",
            "role": Role.SYSTEM_ADMIN.value,
            "type": "access",
            "jti": "abc",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "iss": "wariverse-core-api",
        },
        "an-attackers-key",
        algorithm="HS256",
    )
    with pytest.raises(AppError) as exc:
        decode_token(forged)
    assert exc.value.code == "TOKEN_INVALID"


def test_unsigned_alg_none_token_is_rejected() -> None:
    unsigned = jwt.encode(
        {"sub": "u", "role": Role.SYSTEM_ADMIN.value, "type": "access", "jti": "a",
         "iat": int(time.time()), "exp": int(time.time()) + 600, "iss": "wariverse-core-api"},
        key="",
        algorithm="none",
    )
    with pytest.raises(AppError):
        decode_token(unsigned)


def test_token_with_unknown_role_is_rejected() -> None:
    token = jwt.encode(
        {"sub": "u", "role": "supreme-leader", "type": "access", "jti": "a",
         "iat": int(time.time()), "exp": int(time.time()) + 600, "iss": "wariverse-core-api"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AppError) as exc:
        decode_token(token)
    assert exc.value.code == "TOKEN_INVALID"


# --- MFA -------------------------------------------------------------------
def test_mfa_code_round_trip() -> None:
    import pyotp

    secret = generate_mfa_secret()
    assert verify_mfa_code(secret, pyotp.TOTP(secret).now())
    assert not verify_mfa_code(secret, "000000")
