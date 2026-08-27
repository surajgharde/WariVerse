"""Heritage archive schemas (Track 1, item 5).

Two output shapes, split on the moderation boundary rather than on privacy.

`HeritagePublic` is a published record: the text, who it is by, where it was
collected. `HeritageOut` adds the review state, the reviewer and the rejection
note, and is only ever returned to a moderator or to the person who submitted
it — a contributor is entitled to know their grandmother's ovi was declined and
why, and nobody else is.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.models.heritage import HeritageKind, ReviewState
from app.schemas.common import ApiModel


def _collapse(value: str) -> str:
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ValueError("This cannot be blank")
    return collapsed


class HeritageContribute(ApiModel):
    """What a pilgrim submits.

    Marathi is required and English is not, which is the inverse of most forms
    in most software and the right way round here: an archive of the Wari whose
    canonical text is a translation has already lost what it is preserving.

    Nothing here sets a status. A contribution is `pending` and only a moderator
    moves it — a field the client could set would be the first thing abused.
    """

    kind: HeritageKind
    title_mr: str = Field(min_length=1, max_length=200)
    title_en: str | None = Field(default=None, max_length=200)
    body_mr: str = Field(min_length=2, max_length=20000)
    body_en: str | None = Field(default=None, max_length=20000)

    attribution: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=300)
    era: str | None = Field(default=None, max_length=60)

    media_uri: str | None = Field(default=None, max_length=500)
    media_type: str | None = Field(default=None, pattern="^(audio|image|video)$")

    halt_town_id: uuid.UUID | None = None
    dindi_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)

    #: Who to credit. Often not the person filling in the form — a grandson
    #: submitting his grandmother's ovi should be able to name her.
    contributed_by_name: str | None = Field(default=None, max_length=120)

    _title = field_validator("title_mr")(_collapse)

    @field_validator("tags")
    @classmethod
    def _tidy_tags(cls, value: list[str]) -> list[str]:
        cleaned = {" ".join(tag.split()).lower() for tag in value}
        return sorted(tag for tag in cleaned if tag)


class HeritagePublic(ApiModel):
    """A published record, as a pilgrim reads it."""

    id: uuid.UUID
    kind: HeritageKind
    title_mr: str
    title_en: str | None = None
    body_mr: str
    body_en: str | None = None
    attribution: str | None = None
    source: str | None = None
    era: str | None = None
    media_uri: str | None = None
    media_type: str | None = None
    halt_town_id: uuid.UUID | None = None
    dindi_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list)
    contributed_by_name: str | None = None
    published_at: datetime | None = None


class HeritageOut(HeritagePublic):
    """A record with its review state. Moderators, and the contributor."""

    status: ReviewState
    reviewed_at: datetime | None = None
    #: Kept with the text rather than instead of it — a rejection is not a
    #: reason to destroy the only copy anybody typed.
    review_note: str | None = None
    created_at: datetime | None = None


class HeritageReview(ApiModel):
    """Publish it, or decline it and say why."""

    publish: bool
    note: str | None = Field(default=None, max_length=1000)


class HeritageUpdate(ApiModel):
    """A moderator's correction before publishing — a typo, a missing source."""

    title_mr: str | None = Field(default=None, max_length=200)
    title_en: str | None = Field(default=None, max_length=200)
    body_mr: str | None = Field(default=None, max_length=20000)
    body_en: str | None = Field(default=None, max_length=20000)
    attribution: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=300)
    era: str | None = Field(default=None, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=12)
