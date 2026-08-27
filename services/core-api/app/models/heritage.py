"""Digital preservation of Wari heritage and traditions (Track 1, item 5).

The Wari is eight hundred years old and most of what makes it the Wari is not
written down anywhere: the abhangs a particular Dindi sings on a particular
stretch of road, why the Palkhi halts where it halts, the ringan, the names of
the fields it runs in. It lives in the people walking, and every year some of
them do not walk again.

This is a small, deliberately unambitious archive: a text record with an
attribution, an optional recording, and a moderation gate. What it is not is a
content platform. Three decisions carry that:

**Everything a pilgrim submits is `pending` until a human publishes it.** An
archive of religious tradition that anybody can write into is a vandalism target
and, worse, a place where a plausible invention becomes a citation. The gate is
the feature.

**Attribution is a field, not a courtesy.** `contributed_by_name` and `source`
are what turn a text from folklore into a record. An abhang with no idea who
sang it or where it was collected is not preservation; it is a poster.

**Rejection keeps the text.** A rejected contribution stays with its reason.
Someone's grandmother's version of an ovi being wrong for this archive is not a
reason to destroy the only copy anybody typed.
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


class HeritageKind(StrEnum):
    """What sort of thing this is.

    Chosen so a Warkari would recognise the word, not so a database would. An
    abhang and an ovi are both verse and are not the same thing, and collapsing
    them into `poem` would lose the distinction that matters to the only people
    this archive is for.
    """

    ABHANG = "abhang"  # devotional verse, Tukaram/Dnyaneshwar tradition
    OVI = "ovi"  # the metre women sing at the grindstone
    BHAJAN = "bhajan"
    KIRTAN = "kirtan"  # the performed narration
    STORY = "story"  # oral history, a memory of a past Wari
    RITUAL = "ritual"  # what is done, when, and by whom
    PLACE_LORE = "place_lore"  # why the Palkhi halts here
    DINDI_HISTORY = "dindi_history"
    PHOTO = "photo"


class ReviewState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"


#: The only state a pilgrim-facing read may return.
PUBLIC_STATES: tuple[str, ...] = (ReviewState.PUBLISHED,)


class HeritageItem(Base, TimestampMixin):
    __tablename__ = "heritage_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    title_mr: Mapped[str] = mapped_column(String(200), nullable=False)
    title_en: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: Marathi is the required side, English the optional one — the inverse of
    #: most software, and correct here. A Wari archive whose canonical text is a
    #: translation has already lost the thing it is preserving.
    body_mr: Mapped[str] = mapped_column(Text, nullable=False)
    body_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Who it is *by* or *about* — the saint, the Dindi, the singer.
    attribution: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Where it was collected from. A book, a person, a recording made on the
    #: road. This is the field that makes the row a record.
    source: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: Roughly when it belongs to, in words: "17th century", "before 1972".
    #: A string, not a date: precision this archive does not have would be a
    #: fabrication, and "18th century, approximately" is the honest answer.
    era: Mapped[str | None] = mapped_column(String(60), nullable=True)

    #: An audio recording of it being sung, or a photograph. A URI, never bytes:
    #: this table is read on every archive page and a BYTEA column would make
    #: that read carry megabytes it does not need.
    media_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # audio | image | video

    #: Optional anchors into the live system, so the archive can answer "what is
    #: sung here" when a pilgrim is standing at a halt town.
    halt_town_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("halt_towns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dindi_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("dindis.id", ondelete="SET NULL"), nullable=True, index=True
    )

    tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    # --- provenance and review -------------------------------------------
    contributed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The name to print beside it. Separate from `contributed_by` because the
    #: person who typed it in is often not the person it came from — a grandson
    #: submitting his grandmother's ovi should be able to credit her.
    contributed_by_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=ReviewState.PENDING, index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why it was rejected, kept with the text rather than instead of it.
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_heritage_status_kind", "status", "kind"),
        Index("ix_heritage_published", "status", "published_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<HeritageItem {self.kind} {self.title_mr!r} {self.status}>"
