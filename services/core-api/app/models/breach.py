"""Queue-breach ledger (Section 4/M5).

The point of this table is that it can be *trusted under pressure*.  Each row
carries the hash of the previous row, so removing or editing a record breaks
the chain visibly.  No column here identifies a person — the record says a rule
was broken at a gate, and leaves who to lawful human process.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ReviewStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FALSE_POSITIVE = "false_positive"
    AUTHORISED = "authorised"  # legitimate, with a written reason


class BreachEvent(Base, TimestampMixin):
    __tablename__ = "breach_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Monotonic position in the hash chain.  Gaps are themselves evidence.
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)

    tripwire_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tripwires.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="RESTRICT"), nullable=False
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # in | out
    crossing_count: Mapped[int] = mapped_column(nullable=False, default=1)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Narrow, deliberate exception to "no video is persisted" (Section 12).
    clip_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    clip_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Hash chain: chain_hash = SHA256(prev_hash || canonical(payload))
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Was a valid pass scanned at this gate within ±30s?  If yes there is no
    # event at all; this records the check that was made.
    pass_scan_checked: Mapped[bool] = mapped_column(nullable=False, default=False)
    matched_pass_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("passes.id", ondelete="SET NULL"), nullable=True
    )

    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReviewStatus.PENDING, index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    deletion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_breach_gate_occurred", "gate_id", "occurred_at"),
        Index("ix_breach_review_occurred", "review_status", "occurred_at"),
    )


class ClipAccessLog(Base):
    """Every evidence-clip view, with the purpose the viewer typed."""

    __tablename__ = "clip_access_log"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    breach_event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("breach_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class PurgeLog(Base):
    """Proof that retention was applied, and to what."""

    __tablename__ = "purge_log"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    rows_affected: Mapped[int] = mapped_column(nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
