"""Darshan slots and passes (Section 4/M1)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

MAX_GROUP_SIZE = 6


class SlotStatus(StrEnum):
    OPEN = "open"
    FULL = "full"
    CLOSED = "closed"  # admin-closed, e.g. ritual window
    COMPLETED = "completed"


class PassStatus(StrEnum):
    ACTIVE = "active"
    SCANNED = "scanned"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Slot(Base, TimestampMixin):
    __tablename__ = "slots"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    booked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Never bookable online.  Protects pilgrims without smartphones from being
    # locked out of darshan by digitally literate ones (Section 5, E1).
    walkin_reserve: Mapped[int] = mapped_column(Integer, nullable=False)
    walkin_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Seats an ordinary booking cannot take (Track 1, item 4).
    #
    # Same shape as the walk-in reserve and the same reasoning one step further:
    # a pilgrim who cannot stand in a corridor for four hours should not be
    # competing for the same seat as one who can. Held back from the general
    # pool, released only to a booker with a declared mobility need, and
    # returned to nobody if unused — an empty reserved seat is a smaller failure
    # than an elderly pilgrim sent home.
    assisted_reserve: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assisted_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=SlotStatus.OPEN, index=True)
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gates.id", ondelete="SET NULL"), nullable=True
    )
    # Filled by the scanner: how many people actually passed in this window.
    # The reslotting job compares this against `capacity` (Section 4/M1).
    actual_throughput: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("date", "start_time", "gate_id", name="uq_slot_date_start_gate"),
        CheckConstraint("booked_count >= 0", name="booked_count_non_negative"),
        CheckConstraint("booked_count + walkin_reserve <= capacity", name="no_oversubscription"),
        Index("ix_slots_date_start", "date", "start_time"),
    )

    @property
    def available(self) -> int:
        return max(0, self.capacity - self.booked_count - self.walkin_reserve)


class Pass(Base, TimestampMixin):
    __tablename__ = "passes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    slot_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("slots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # HMAC, never the raw number (Section 12).
    holder_phone_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    holder_name: Mapped[str] = mapped_column(String(120), nullable=False)
    holder_language: Mapped[str] = mapped_column(String(5), nullable=False, default="mr")
    group_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Seed for the rotating QR code.  Never leaves the server except inside a
    # signed token the holder's own device renders.
    qr_secret: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=PassStatus.ACTIVE, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    scanned_gate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gates.id", ondelete="SET NULL"), nullable=True
    )

    original_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("slots.id", ondelete="SET NULL"), nullable=True
    )
    reslot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Opt-in, default off.  A pass may never be moved *earlier* without this:
    # people travel, and an earlier slot they cannot reach is a false promise
    # that costs them their place (Section 4/M1).
    allow_early_reslot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(f"group_size >= 1 AND group_size <= {MAX_GROUP_SIZE}", name="group_size_range"),
        Index("ix_passes_slot_status", "slot_id", "status"),
        Index("ix_passes_phone_status", "holder_phone_hash", "status"),
    )


class PassMember(Base):
    """Named companions on a group pass.  One QR, one scan, up to 6 people."""

    __tablename__ = "pass_members"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pass_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("passes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    age_band: Mapped[str | None] = mapped_column(String(12), nullable=True)  # child | adult | senior


class PassNotification(Base, TimestampMixin):
    """Outbox for reslot/reminder messages.  Queued here, sent by the notifier."""

    __tablename__ = "pass_notifications"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pass_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("passes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # reslot | reminder | cancelled | expiring
    channel: Mapped[str] = mapped_column(String(12), nullable=False, default="push")  # push | sms | ivr
    payload_mr: Mapped[str] = mapped_column(Text, nullable=False)
    payload_en: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
