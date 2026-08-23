"""Users and the encrypted contact table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.permissions import Role
from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Staff accounts keep a raw phone (they are employees, not pilgrims, and
    # they consent as part of employment).  Pilgrims are identified by
    # phone_hash only — see `passes.holder_phone_hash`.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.PILGRIM, index=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default="mr")

    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_login_count: Mapped[int] = mapped_column(nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_users_role_active", "role", "is_active"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.name} role={self.role}>"


class ContactSecret(Base, TimestampMixin):
    """Raw phone numbers, encrypted, with a TTL (Section 12 data minimisation).

    Exists only so notification delivery can reach a pilgrim while their pass is
    live.  `purge_after` is enforced by a scheduled job, not by hope.
    """

    __tablename__ = "contact_secrets"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    encrypted_phone: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)  # pass_notification | incident_contact
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
