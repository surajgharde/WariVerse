"""User administration schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.permissions import PASSWORD_LOGIN_ROLES, Role
from app.schemas.auth import _validate_phone
from app.schemas.common import ApiModel


class UserCreate(ApiModel):
    phone: str
    name: str = Field(min_length=2, max_length=120)
    role: Role
    language: str = "mr"
    password: str | None = Field(default=None, min_length=12, max_length=200)

    _check_phone = field_validator("phone")(_validate_phone)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.isalpha() or value.isdigit():
            raise ValueError("Use a mix of letters, digits and symbols")
        return value

    def requires_password(self) -> bool:
        return self.role in PASSWORD_LOGIN_ROLES


class UserUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    role: Role | None = None
    language: str | None = None
    is_active: bool | None = None


class UserOut(ApiModel):
    id: str
    name: str
    role: Role
    language: str
    is_active: bool
    phone_masked: str | None = None
    mfa_enrolled: bool = False
    last_login_at: datetime | None = None
    created_at: datetime


class AuditEntryOut(ApiModel):
    id: str
    actor_id: str | None
    actor_role: str | None
    action: str
    target_type: str | None
    target_id: str | None
    meta: dict[str, Any]
    ip: str | None
    trace_id: str | None
    created_at: datetime


class ConfigEntryOut(ApiModel):
    key: str
    value: object
    description: str
    updated_at: datetime


class ConfigUpdate(ApiModel):
    value: object
    reason: str = Field(min_length=4, max_length=500)
