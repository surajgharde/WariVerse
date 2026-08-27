"""Auth request/response schemas."""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from app.core.permissions import Role
from app.schemas.common import ApiModel

# Accepts 9876543210, 09876543210, +91 98765 43210, 91-9876543210.
_PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{10,17}$")
_LANGS = {"mr", "hi", "en"}


def _validate_phone(value: str) -> str:
    value = value.strip()
    if not _PHONE_RE.match(value):
        raise ValueError("Enter a valid phone number")
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 10:
        raise ValueError("Enter a valid phone number")
    return value


class OtpRequest(ApiModel):
    phone: str
    purpose: str = Field(default="login", pattern="^(login|pass_booking|incident_contact)$")

    _check_phone = field_validator("phone")(_validate_phone)


class OtpRequestResponse(ApiModel):
    sent: bool
    expires_in: int
    # Present only when OTP_DEBUG_ECHO is on (development).  Section 12 forbids
    # this in any deployed environment.
    debug_code: str | None = None


class OtpVerify(ApiModel):
    phone: str
    code: str = Field(min_length=4, max_length=8)
    name: str | None = Field(default=None, max_length=120)
    language: str = "mr"

    _check_phone = field_validator("phone")(_validate_phone)

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        return value if value in _LANGS else "mr"


class NameLogin(ApiModel):
    """Pilgrim sign-in with nothing but a name.

    There is no SMS gateway wired up, so an OTP a pilgrim never receives is a
    door that does not open.  The name is HMACed into the same identity column a
    phone hash would go in, which is what makes the second sign-in land on the
    first sign-in's account and its passes.
    """

    name: str = Field(min_length=1, max_length=120)
    language: str = "mr"

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter your name")
        return value

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        return value if value in _LANGS else "mr"


class PasswordLogin(ApiModel):
    phone: str
    password: str = Field(min_length=8, max_length=200)

    _check_phone = field_validator("phone")(_validate_phone)


class DevLogin(ApiModel):
    """Development sign-in. Just the phone — no password, no TOTP.

    The route that consumes this is dead unless `ENVIRONMENT=development` *and*
    `DEV_LOGIN_ENABLED=true`, and the app refuses to boot in production with the
    flag on. See `auth.dev_login`.
    """

    phone: str


class MfaVerify(ApiModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RefreshRequest(ApiModel):
    refresh_token: str


class LogoutRequest(ApiModel):
    refresh_token: str | None = None


class UserProfile(ApiModel):
    id: str
    name: str
    role: Role
    language: str
    permissions: list[str]
    mfa_enrolled: bool
    phone_masked: str | None = None


class TokenResponse(ApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserProfile


class MfaChallengeResponse(ApiModel):
    mfa_required: bool = True
    mfa_token: str
    expires_in: int = 300


class MfaEnrolResponse(ApiModel):
    secret: str
    provisioning_uri: str
    message: str = "Scan this in an authenticator app, then confirm with a code."
