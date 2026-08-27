"""End-to-end auth: OTP sign-in, staff login, MFA, refresh rotation, RBAC.

These run against real Postgres and Redis — Section 0 forbids proving auth works
against a stub.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import generate_mfa_secret
from app.models import AuditLog, User
from app.services.audit_service import AuditAction

pytestmark = [pytest.mark.db, pytest.mark.redis]

PILGRIM_PHONE = "9876543210"
STAFF_PHONE = "9820011223"
ADMIN_PHONE = "9820044556"
STAFF_PASSWORD = "correct-horse-battery-staple"


async def _sign_in_pilgrim(client: AsyncClient, api_prefix: str, phone: str = PILGRIM_PHONE) -> dict:
    requested = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": phone})
    assert requested.status_code == 200, requested.text
    code = requested.json()["debug_code"]
    assert code, "OTP_DEBUG_ECHO must be on in the test environment"

    verified = await client.post(
        f"{api_prefix}/auth/otp/verify",
        json={"phone": phone, "code": code, "name": "संत ज्ञानेश्वर भक्त", "language": "mr"},
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


# --- OTP flow --------------------------------------------------------------
async def test_otp_sign_in_creates_a_pilgrim_and_returns_tokens(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    body = await _sign_in_pilgrim(client, api_prefix)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["role"] == Role.PILGRIM
    assert body["user"]["language"] == "mr"
    assert "pass:book" in body["user"]["permissions"]

    user = (await session.execute(select(User))).scalar_one()
    # Section 12: a pilgrim's raw number is never stored.
    assert user.phone is None
    assert len(user.phone_hash) == 64


async def test_same_phone_in_a_different_format_reuses_the_account(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    await _sign_in_pilgrim(client, api_prefix, "9876543210")
    await _sign_in_pilgrim(client, api_prefix, "+91 98765 43210")
    assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_wrong_otp_is_rejected_with_the_error_envelope(client: AsyncClient, api_prefix: str) -> None:
    await client.post(f"{api_prefix}/auth/otp/request", json={"phone": PILGRIM_PHONE})
    response = await client.post(
        f"{api_prefix}/auth/otp/verify", json={"phone": PILGRIM_PHONE, "code": "000000"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "OTP_INVALID"
    assert error["message_mr"]  # Marathi is not optional
    assert error["trace_id"]
    assert error["details"]["attempts_remaining"] == 4


async def test_otp_cannot_be_replayed(client: AsyncClient, api_prefix: str) -> None:
    requested = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": PILGRIM_PHONE})
    code = requested.json()["debug_code"]
    first = await client.post(f"{api_prefix}/auth/otp/verify", json={"phone": PILGRIM_PHONE, "code": code})
    assert first.status_code == 200

    second = await client.post(f"{api_prefix}/auth/otp/verify", json={"phone": PILGRIM_PHONE, "code": code})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "OTP_EXPIRED"


async def test_otp_requests_are_limited_to_three_per_hour(client: AsyncClient, api_prefix: str) -> None:
    for _ in range(3):
        ok = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": PILGRIM_PHONE})
        assert ok.status_code == 200

    limited = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": PILGRIM_PHONE})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.json()["error"]["details"]["limit"] == 3


# --- name sign-in ----------------------------------------------------------
async def test_name_login_creates_a_pilgrim_and_returns_tokens(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    response = await client.post(
        f"{api_prefix}/auth/name-login", json={"name": "तुकाराम महाराज", "language": "mr"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["role"] == Role.PILGRIM
    assert body["user"]["name"] == "तुकाराम महाराज"
    assert "pass:book" in body["user"]["permissions"]

    user = (await session.execute(select(User))).scalar_one()
    # No phone was collected, so there is nothing to store or leak.
    assert user.phone is None
    assert len(user.phone_hash) == 64


async def test_name_login_returns_to_the_same_account(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    """Otherwise a pilgrim signing in again loses the pass they booked."""
    first = await client.post(f"{api_prefix}/auth/name-login", json={"name": "Tukaram Maharaj"})
    second = await client.post(f"{api_prefix}/auth/name-login", json={"name": "  tukaram   maharaj "})
    assert second.status_code == 200, second.text
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_name_login_cannot_reach_a_staff_account(
    client: AsyncClient, api_prefix: str, make_user
) -> None:
    """A staff member's name is public; their account must not be."""
    staff = await make_user(phone=STAFF_PHONE, role=Role.SECURITY_OFFICER, password=STAFF_PASSWORD)
    response = await client.post(f"{api_prefix}/auth/name-login", json={"name": staff.name})
    assert response.status_code == 200, response.text
    # A brand new pilgrim, not the officer.
    assert response.json()["user"]["id"] != str(staff.id)
    assert response.json()["user"]["role"] == Role.PILGRIM


async def test_name_login_rejects_a_blank_name(client: AsyncClient, api_prefix: str) -> None:
    response = await client.post(f"{api_prefix}/auth/name-login", json={"name": "   "})
    assert response.status_code == 422


# --- staff login -----------------------------------------------------------
async def test_staff_login_with_password(client: AsyncClient, api_prefix: str, make_user) -> None:
    await make_user(phone=STAFF_PHONE, role=Role.SECURITY_OFFICER, password=STAFF_PASSWORD)
    response = await client.post(
        f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": STAFF_PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == Role.SECURITY_OFFICER


async def test_staff_cannot_sign_in_with_otp(client: AsyncClient, api_prefix: str, make_user) -> None:
    """A privileged account must not be reachable with a code sent over SMS."""
    await make_user(phone=STAFF_PHONE, role=Role.SECURITY_OFFICER, password=STAFF_PASSWORD)
    requested = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": STAFF_PHONE})
    code = requested.json()["debug_code"]
    response = await client.post(
        f"{api_prefix}/auth/otp/verify", json={"phone": STAFF_PHONE, "code": code}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_wrong_password_locks_the_account_after_five_attempts(
    client: AsyncClient, session: AsyncSession, api_prefix: str, make_user
) -> None:
    user = await make_user(phone=STAFF_PHONE, role=Role.VOLUNTEER, password=STAFF_PASSWORD)
    for _ in range(5):
        failed = await client.post(
            f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": "wrong-password-here"}
        )
        assert failed.status_code == 401

    await session.refresh(user)
    assert user.locked_until is not None

    # Correct password now also refused while the lockout stands.
    blocked = await client.post(
        f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": STAFF_PASSWORD}
    )
    assert blocked.status_code == 401
    assert "locked_until" in blocked.json()["error"]["details"]


async def test_unknown_account_and_wrong_password_look_the_same(
    client: AsyncClient, api_prefix: str, make_user
) -> None:
    await make_user(phone=STAFF_PHONE, role=Role.VOLUNTEER, password=STAFF_PASSWORD)
    unknown = await client.post(
        f"{api_prefix}/auth/login", json={"phone": "9000000000", "password": STAFF_PASSWORD}
    )
    wrong = await client.post(
        f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": "not-the-password"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"


# --- MFA -------------------------------------------------------------------
async def test_administrator_login_returns_an_mfa_challenge_not_tokens(
    client: AsyncClient, api_prefix: str, make_user
) -> None:
    await make_user(
        phone=ADMIN_PHONE, role=Role.ADMINISTRATOR, password=STAFF_PASSWORD, mfa_secret=generate_mfa_secret()
    )
    response = await client.post(
        f"{api_prefix}/auth/login", json={"phone": ADMIN_PHONE, "password": STAFF_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True
    assert "access_token" not in body


async def test_administrator_completes_mfa_and_gets_tokens(
    client: AsyncClient, api_prefix: str, make_user
) -> None:
    import pyotp

    secret = generate_mfa_secret()
    await make_user(phone=ADMIN_PHONE, role=Role.ADMINISTRATOR, password=STAFF_PASSWORD, mfa_secret=secret)
    challenge = await client.post(
        f"{api_prefix}/auth/login", json={"phone": ADMIN_PHONE, "password": STAFF_PASSWORD}
    )
    mfa_token = challenge.json()["mfa_token"]

    wrong = await client.post(
        f"{api_prefix}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"}
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "MFA_INVALID"

    ok = await client.post(
        f"{api_prefix}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["role"] == Role.ADMINISTRATOR


# --- refresh rotation ------------------------------------------------------
async def test_refresh_rotates_the_token(client: AsyncClient, api_prefix: str) -> None:
    tokens = await _sign_in_pilgrim(client, api_prefix)
    refreshed = await client.post(
        f"{api_prefix}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]


async def test_replaying_an_old_refresh_token_kills_the_session(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    """The stolen-token case.  Using a superseded refresh token must revoke the
    whole family, not just fail once."""
    tokens = await _sign_in_pilgrim(client, api_prefix)
    original = tokens["refresh_token"]

    rotated = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": original})
    current = rotated.json()["refresh_token"]

    replayed = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": original})
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "TOKEN_REUSED"

    # The legitimate holder is signed out too — that is the point.
    after = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": current})
    assert after.status_code == 401

    reuse_events = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.TOKEN_REUSE_DETECTED)
    )
    assert reuse_events == 1


async def test_logout_denies_the_access_token_immediately(client: AsyncClient, api_prefix: str, auth_headers) -> None:
    tokens = await _sign_in_pilgrim(client, api_prefix)
    headers = auth_headers(tokens["access_token"])

    assert (await client.get(f"{api_prefix}/auth/me", headers=headers)).status_code == 200

    out = await client.post(
        f"{api_prefix}/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )
    assert out.status_code == 200

    after = await client.get(f"{api_prefix}/auth/me", headers=headers)
    assert after.status_code == 401


# --- RBAC ------------------------------------------------------------------
async def test_pilgrim_cannot_read_the_audit_log(client: AsyncClient, api_prefix: str, auth_headers) -> None:
    tokens = await _sign_in_pilgrim(client, api_prefix)
    response = await client.get(f"{api_prefix}/audit", headers=auth_headers(tokens["access_token"]))
    assert response.status_code == 403
    details = response.json()["error"]["details"]
    assert details["role"] == Role.PILGRIM
    assert "audit:view" in details["missing_permissions"]


async def test_unauthenticated_request_is_rejected(client: AsyncClient, api_prefix: str) -> None:
    response = await client.get(f"{api_prefix}/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_security_officer_still_cannot_manage_users(
    client: AsyncClient, api_prefix: str, auth_headers, make_user
) -> None:
    await make_user(phone=STAFF_PHONE, role=Role.SECURITY_OFFICER, password=STAFF_PASSWORD)
    login = await client.post(
        f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": STAFF_PASSWORD}
    )
    headers = auth_headers(login.json()["access_token"])
    assert (await client.get(f"{api_prefix}/users", headers=headers)).status_code == 403


# --- audit trail -----------------------------------------------------------
async def test_sign_in_writes_an_audit_entry(
    client: AsyncClient, session: AsyncSession, api_prefix: str
) -> None:
    await _sign_in_pilgrim(client, api_prefix)
    rows = (await session.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars().all()
    actions = [row.action for row in rows]
    assert AuditAction.OTP_REQUESTED in actions
    assert AuditAction.OTP_VERIFIED in actions
    assert all(row.trace_id for row in rows)


async def test_audit_entries_never_contain_credentials(
    client: AsyncClient, session: AsyncSession, api_prefix: str, make_user
) -> None:
    await make_user(phone=STAFF_PHONE, role=Role.VOLUNTEER, password=STAFF_PASSWORD)
    await client.post(f"{api_prefix}/auth/login", json={"phone": STAFF_PHONE, "password": "wrong-password"})
    rows = (await session.execute(select(AuditLog))).scalars().all()
    dumped = " ".join(str(row.meta) for row in rows)
    assert STAFF_PASSWORD not in dumped
    assert "wrong-password" not in dumped
