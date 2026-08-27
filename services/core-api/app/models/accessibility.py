"""Accessibility: who needs help, what help, and whether it arrived.

Track 1 item 4 asks for "inclusive solutions for elderly and differently-abled
Warkaris". The Wari's median pilgrim is old and walking; a system that treats
accessibility as a checkbox on a form has not understood its own users.

Three things live here, and they are separate on purpose.

**A profile is standing.** `AccessibilityProfile` is what a pilgrim needs in
general — a wheelchair, a companion, large text, a step-free route. It is
attached to the user and outlives any one pass, because re-declaring a
disability on every booking is precisely the indignity this is meant to remove.

**A request is an event.** `AssistanceRequest` is "I need a wheelchair at Gate 3
now". It has a clock, an assignee and an outcome, like an incident — because
that is what it is. It is deliberately *not* an incident row: grading a
wheelchair request against a cardiac arrest on one SLA board would either
under-serve the emergency or drown the desk in low-severity noise, and the
dispatch rules differ (a volunteer with a chair, not an ambulance).

**A reserve is capacity.** Held on `slots`, not here — see migration 0009. The
reserve is the part that actually changes who gets darshan: seats that ordinary
bookings cannot consume, so a pilgrim who cannot queue for four hours is not
competing for the same seat as someone who can.

Privacy note. A declared disability is health data, and this table is the most
sensitive non-photo store in the system after `missing_persons`. It is never
returned in a list endpoint, never broadcast over the socket, and
`needs_summary` exists so a volunteer being dispatched learns "wheelchair" and
not the pilgrim's medical notes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AssistanceNeed(StrEnum):
    """What kind of help, in terms a volunteer can act on.

    Named for the *help required*, not the condition. "Wheelchair" tells a
    volunteer what to bring; "paraplegia" tells them something private and
    leaves them still guessing. Every value here answers "what do I do".
    """

    WHEELCHAIR = "wheelchair"
    WALKING_SUPPORT = "walking_support"  # a stick, an arm, a slower pace
    STRETCHER = "stretcher"
    VISION = "vision"  # guide, spoken directions
    HEARING = "hearing"  # written or signed, not shouted
    SPEECH = "speech"
    COGNITIVE = "cognitive"  # needs simple instructions, may be disoriented
    COMPANION_REQUIRED = "companion_required"  # must not be separated
    STEP_FREE_ROUTE = "step_free_route"
    OXYGEN = "oxygen"


#: Needs that mean a pilgrim cannot use the ordinary queue at all, and so draw
#: on the reserved darshan capacity rather than the general pool.
MOBILITY_NEEDS: frozenset[str] = frozenset(
    {
        AssistanceNeed.WHEELCHAIR,
        AssistanceNeed.STRETCHER,
        AssistanceNeed.WALKING_SUPPORT,
        AssistanceNeed.STEP_FREE_ROUTE,
        AssistanceNeed.OXYGEN,
    }
)


class RequestStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    MET = "met"
    CANCELLED = "cancelled"
    UNMET = "unmet"  # closed without help arriving — recorded, never hidden


#: Minutes from request to a volunteer being assigned. One number, not a matrix:
#: these requests are not graded against each other, because a wheelchair that
#: is twenty minutes late is the same failure whoever asked for it.
ASSISTANCE_SLA_MINUTES = 15


class AccessibilityProfile(Base, TimestampMixin):
    """What one pilgrim needs, standing.

    One row per user, so a pilgrim declares it once. `needs` is a JSONB array of
    `AssistanceNeed` values rather than a join table: it is read whole, written
    whole, never queried by individual element, and a five-row join table for a
    list a human ticks in one screen is machinery with no purpose.
    """

    __tablename__ = "accessibility_profiles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    needs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: Free text in the pilgrim's own words — "left leg, cannot climb steps".
    #: Shown to a volunteer only when they are actually assigned to help.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Interface preferences. Not medical, and the reason they live beside the
    #: needs is that a pilgrim who ticks "vision" almost always wants the larger
    #: type too, and asking twice in two places is asking twice.
    large_text: Mapped[bool] = mapped_column(nullable=False, default=False)
    high_contrast: Mapped[bool] = mapped_column(nullable=False, default=False)

    #: Somebody to call who is not the pilgrim. Hash only (Section 12).
    companion_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def has_mobility_need(self) -> bool:
        return any(need in MOBILITY_NEEDS for need in (self.needs or []))


class AssistanceRequest(Base, TimestampMixin):
    """One ask for help, with a clock on it."""

    __tablename__ = "assistance_requests"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: A volunteer may raise one on somebody's behalf — the person who needs the
    #: chair is frequently not the person holding a phone.
    on_behalf_of: Mapped[str | None] = mapped_column(String(120), nullable=True)

    needs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gates.id", ondelete="SET NULL"), nullable=True
    )
    facility_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("facilities.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=RequestStatus.OPEN, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    #: When a volunteer must have been assigned by. Stored rather than derived
    #: so the board can sort on it in SQL, the same as `incidents.sla_due_at`.
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: What actually happened. Required to close as `unmet`, because "nobody
    #: came" is the outcome most worth being able to count afterwards.
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    language: Mapped[str] = mapped_column(String(5), nullable=False, default="mr")

    __table_args__ = (
        Index("ix_assistance_status_due", "status", "sla_due_at"),
        Index("ix_assistance_zone_status", "zone_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AssistanceRequest {self.reference} {self.status}>"


#: Statuses that still need somebody.
OPEN_REQUEST_STATUSES: tuple[str, ...] = (RequestStatus.OPEN, RequestStatus.ASSIGNED)
