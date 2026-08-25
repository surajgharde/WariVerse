"""The development sign-in, and the locks on it.

A route that hands out administrator tokens without a password is exactly the
kind of convenience that ships by accident, so the tests that matter here are
the ones about it *not* working. Three of the four below assert a refusal.

The production guard is a pure-function test on `Settings`, so it runs without
Docker — the check that stops this reaching production must not be the one that
gets skipped on a laptop.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.permissions import Role
from app.core.security import create_access_token


# ---------------------------------------------------------------------------
# the production guard — pure, always runs
# ---------------------------------------------------------------------------
def test_production_refuses_to_boot_with_dev_login_on():
    """`assert_production_safe` is what makes this survivable.

    The same treatment `OTP_DEBUG_ECHO` gets: the application does not merely
    ignore the flag in production, it refuses to start and says why.
    """
    settings = Settings(
        environment="production",
        dev_login_enabled=True,
        jwt_secret="a-real-looking-secret-for-this-test-0000000",
        phone_hash_secret="another-real-looking-secret-for-this-test-0",
        ai_service_token="a-real-looking-service-token",
        qr_signing_secret="a-real-looking-qr-secret-for-this-test-0000",
        contact_encryption_key="dGVzdC1jb250YWN0LWtleS10ZXN0LWNvbnRhY3Qta2V5PQ==",
        otp_debug_echo=False,
    )

    problems = settings.assert_production_safe()

    assert any("DEV_LOGIN_ENABLED" in p for p in problems)
    assert any("without a password" in p for p in problems)


def test_dev_login_is_off_by_default():
    """A fresh checkout has the route dead until somebody turns it on.

    `_env_file=None` matters here. `Settings` reads `.env`, and the developer
    running this almost certainly has `DEV_LOGIN_ENABLED=true` in theirs — so
    without it this test would assert that *this machine* has the flag off,
    which is not the claim. The claim is about the code's default.
    """
    # `_env_file` is pydantic-settings' documented init override; mypy does not
    # see it because `BaseSettings.__init__` is typed from the model fields.
    fresh = Settings(environment="development", _env_file=None)  # type: ignore[call-arg]
    assert fresh.dev_login_enabled is False


def test_a_clean_production_config_has_no_complaint_about_dev_login():
    settings = Settings(
        environment="production",
        dev_login_enabled=False,
        jwt_secret="a-real-looking-secret-for-this-test-0000000",
        phone_hash_secret="another-real-looking-secret-for-this-test-0",
        ai_service_token="a-real-looking-service-token",
        qr_signing_secret="a-real-looking-qr-secret-for-this-test-0000",
        contact_encryption_key="dGVzdC1jb250YWN0LWtleS10ZXN0LWNvbnRhY3Qta2V5PQ==",
        otp_debug_echo=False,
    )
    assert not any("DEV_LOGIN" in p for p in settings.assert_production_safe())


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------
integration = [pytest.mark.db, pytest.mark.redis]


@pytest.mark.parametrize("phone", ["9000000001", "9000000002", "9000000003"])
@pytest.mark.db
@pytest.mark.redis
async def test_dev_login_signs_in_a_seeded_staff_account(
    client: AsyncClient, api_prefix: str, make_user, monkeypatch, phone: str
):
    """Including the two MFA roles, which is the whole point.

    An Administrator with no enrolled secret cannot use the ordinary path at
    all — `login_with_password` refuses, and `/auth/mfa/enrol` needs a token
    you can only get by signing in. This route is the way out of that deadlock.
    """
    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", True)
    monkeypatch.setattr(live, "environment", "development")

    role = {
        "9000000001": Role.SYSTEM_ADMIN,
        "9000000002": Role.ADMINISTRATOR,
        "9000000003": Role.SECURITY_OFFICER,
    }[phone]
    await make_user(phone=phone, role=role, name="Seeded")

    response = await client.post(f"{api_prefix}/auth/dev-login", json={"phone": phone})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["user"]["role"] == role

    # The token is usable immediately — no MFA step, which is the convenience.
    me = await client.get(
        f"{api_prefix}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200


@pytest.mark.db
@pytest.mark.redis
async def test_dev_login_is_a_404_when_the_flag_is_off(
    client: AsyncClient, api_prefix: str, make_user, monkeypatch
):
    """404, not 403.

    A route that does not exist outside development should not advertise that
    it exists — a 403 tells a prober there is something here worth finding.
    """
    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", False)
    await make_user(phone="9000000003", role=Role.SECURITY_OFFICER, name="Seeded")

    response = await client.post(f"{api_prefix}/auth/dev-login", json={"phone": "9000000003"})
    assert response.status_code == 404


@pytest.mark.db
@pytest.mark.redis
async def test_dev_login_is_refused_outside_development_even_with_the_flag_on(
    client: AsyncClient, api_prefix: str, make_user, monkeypatch
):
    """Two independent locks. Setting the flag in staging is not enough."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", True)
    monkeypatch.setattr(live, "environment", "staging")
    await make_user(phone="9000000003", role=Role.SECURITY_OFFICER, name="Seeded")

    response = await client.post(f"{api_prefix}/auth/dev-login", json={"phone": "9000000003"})
    assert response.status_code == 404


@pytest.mark.db
@pytest.mark.redis
async def test_dev_login_will_not_mint_a_pilgrim_token(
    client: AsyncClient, api_prefix: str, make_user, monkeypatch
):
    """Pilgrims sign in by OTP, and `OTP_DEBUG_ECHO` already makes that easy in
    development. A second, less visible path to a pilgrim token is not a
    convenience worth having."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", True)
    monkeypatch.setattr(live, "environment", "development")
    await make_user(phone="9876543210", role=Role.PILGRIM, password=None, name="यात्रेकरू")

    response = await client.post(f"{api_prefix}/auth/dev-login", json={"phone": "9876543210"})
    assert response.status_code == 401


@pytest.mark.db
@pytest.mark.redis
async def test_a_dev_login_is_marked_as_one_in_the_audit_log(
    client: AsyncClient, api_prefix: str, session, make_user, monkeypatch
):
    """Without this, a demo session and a genuine administrator action are the
    same row in the audit log six months later."""
    from sqlalchemy import text

    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", True)
    monkeypatch.setattr(live, "environment", "development")
    await make_user(phone="9000000002", role=Role.ADMINISTRATOR, name="मंदिर प्रशासक")

    await client.post(f"{api_prefix}/auth/dev-login", json={"phone": "9000000002"})

    row = (
        await session.execute(
            text("SELECT meta FROM audit_log WHERE action = 'auth.login.success' ORDER BY created_at DESC")
        )
    ).first()
    assert row is not None
    assert row[0]["dev_login"] is True
    assert row[0]["mfa_bypassed"] is True


@pytest.mark.db
@pytest.mark.redis
async def test_an_unknown_phone_is_refused(
    client: AsyncClient, api_prefix: str, monkeypatch
):
    from app.core.config import settings as live

    monkeypatch.setattr(live, "dev_login_enabled", True)
    monkeypatch.setattr(live, "environment", "development")

    response = await client.post(f"{api_prefix}/auth/dev-login", json={"phone": "9999999999"})
    assert response.status_code == 401


# `create_access_token` is imported so the module fails loudly if the security
# helpers move; the dev route depends on the same token shape as real sign-in.
assert callable(create_access_token)
