"""Lost and found property (Track 1, item 2).

`missing_persons` already covers the half of lost-and-found that is a person.
This is the other half: a bag, a phone, a bundle of documents, a walking stick.
It is a separate table from `incidents` for the same reason missing persons is —
the data is different in kind. An item has a category, a colour, a mark only its
owner would know, and a physical location where it is now being *held*, and none
of those are fields a crowd-density incident has any use for.

Three rules shape the schema, and each one is a fraud rule before it is a
convenience:

**The distinguishing mark is the password.** `distinguishing_marks` is never
returned by the pilgrim-facing search. A found-item register that publishes
"black Samsung phone, cracked corner, Gate 3" is a list of things to walk up and
claim. The public search answers "is there *a* phone handed in near Gate 3
today", and the mark is what the claimant has to produce from memory at the
desk. `lostfound_service.verify_claim` compares it; the desk never reads it out.

**Custody is a place, not a status.** A found item carries
`custody_facility_id` — which help desk physically holds it. "Found" and "in the
volunteer's pocket" are the same status and very different situations, and a
pilgrim being sent to the wrong desk is the failure this table exists to stop.

**Nothing is deleted; it expires.** `purge_after` starts at *closure*, matching
`missing_persons` — an unclaimed item is still somebody's, and the photo of it
is the only way they will recognise it. An open case never purges.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LostFoundKind(StrEnum):
    """Which side of the desk a record came from."""

    LOST = "lost"  # a pilgrim reporting something gone
    FOUND = "found"  # a volunteer registering something handed in


class ItemCategory(StrEnum):
    """Deliberately coarse.

    Matching is only as good as the category two strangers independently pick
    for the same object, so the list is short and physical. `WALKING_AID` is not
    lumped into `OTHER` on purpose — a lost walking stick or wheelchair is a
    mobility emergency for the pilgrim it belongs to, not lost property, and
    `lostfound_service` gives it the shortest matching window and the highest
    priority for exactly that reason (Track 1, item 4).
    """

    BAG = "bag"
    PHONE = "phone"
    DOCUMENTS = "documents"
    MONEY_PURSE = "money_purse"
    JEWELLERY = "jewellery"
    FOOTWEAR = "footwear"
    CLOTHING = "clothing"
    MEDICINE = "medicine"
    WALKING_AID = "walking_aid"
    RELIGIOUS_ITEM = "religious_item"
    OTHER = "other"


class LostFoundStatus(StrEnum):
    OPEN = "open"  # reported, nothing matched yet
    MATCHED = "matched"  # a staff member has linked it to its counterpart
    CLAIMED = "claimed"  # somebody has come to the desk and verified
    RETURNED = "returned"  # physically handed over — the terminal success
    CLOSED_UNRESOLVED = "closed_unresolved"
    EXPIRED = "expired"  # held past the retention window, never claimed


#: Statuses where somebody is still looking or something is still being held.
OPEN_STATUSES: tuple[str, ...] = (LostFoundStatus.OPEN, LostFoundStatus.MATCHED)

#: Days a closed record (and its photo) is kept before the purge job takes it.
#: Same 30 days as `missing_persons`, and for the same reason: the clock starts
#: at closure, so an unclaimed bag does not lose its photo while its owner is
#: still walking back from Pandharpur.
RETENTION_DAYS = 30


class LostFoundItem(Base, TimestampMixin):
    __tablename__ = "lost_found_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Short human reference the desk reads aloud — `LF-7F3K2A`. A pilgrim on a
    #: 2G phone quoting a UUID over a crowd is not a workable interaction.
    reference: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(8), nullable=False, index=True)  # lost | found
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    #: Safe to show publicly: "blue cloth bag". Enough to recognise, not enough
    #: to claim.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    colour: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: NEVER returned by the public search. See the module docstring.
    distinguishing_marks: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Where it was lost (kind=lost) or picked up (kind=found).
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: When it went missing / was picked up. Not the same as `reported_at` — a
    #: bag lost at the morning aarti is often reported at dusk, and matching on
    #: the report time instead of the loss time is what makes a system suggest
    #: an item found four hours before it was dropped.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    #: Which help desk physically holds a found item. Null for a lost report.
    custody_facility_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("facilities.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reported_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: HMAC only (Section 12). The raw number, if we have one at all, lives in
    #: `contact_secrets` with a TTL.
    reporter_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default="mr")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LostFoundStatus.OPEN, index=True
    )
    #: The counterpart record once a human has confirmed the pair. Nullable and
    #: symmetric — both rows point at each other, so either side of the desk can
    #: be looked up first.
    matched_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lost_found_items.id", ondelete="SET NULL"), nullable=True
    )

    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # --- handover ---------------------------------------------------------
    #: Who took it away, and who watched them do it. Both are required to reach
    #: `returned`: an item that left the desk with no staff name against it is
    #: indistinguishable from an item that was stolen from the desk.
    claimed_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    claimed_by_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handed_over_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: How the claimant proved it was theirs, in the volunteer's words.
    handover_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_lostfound_kind_status", "kind", "status"),
        Index("ix_lostfound_category_occurred", "category", "occurred_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LostFoundItem {self.reference} {self.kind}/{self.category} {self.status}>"


class LostFoundMatch(Base, TimestampMixin):
    """A *suggested* pairing, and what a human did about it.

    Suggestions are stored rather than recomputed on every read, because the
    interesting question after a Wari is not "what would the scorer say today"
    but "what did it say at the time, and did the desk agree". A rejected
    suggestion is the most useful row in this table: it is the only evidence that
    the scoring is wrong in a particular way.
    """

    __tablename__ = "lost_found_matches"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lost_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lost_found_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    found_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lost_found_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 0–100. Never auto-accepted at any value — see `lostfound_service`.
    score: Mapped[float] = mapped_column(Float, nullable=False)
    #: Why, in machine-readable pieces, so the desk sees "same category, same
    #: zone, four hours apart" rather than "87%".
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    suggested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: pending | accepted | rejected
    decision: Mapped[str] = mapped_column(String(12), nullable=False, default="pending", index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("lost_item_id", "found_item_id", name="uq_lostfound_pair"),
        Index("ix_lostfound_match_decision", "decision", "score"),
    )
