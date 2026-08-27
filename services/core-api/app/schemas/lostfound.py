"""Lost-and-found property schemas (Track 1, item 2).

There are two output shapes for the same row, and the difference between them is
the entire privacy design of this module.

`LostFoundPublic` is what a pilgrim searching the found register sees: category,
coarse zone, the day, and a description safe to read in public. `LostFoundOut`
adds the identifying mark, the photo, the reporter and the custody desk, and is
returned only to staff or to the person who filed the record.

The pilgrim-facing search deliberately answers a weaker question than it could.
"Is there a phone handed in near Gate 3 today" is enough to make somebody walk to
the desk. "Black Samsung, cracked top-left corner, held at Desk 2" is enough to
claim a phone that is not yours, and no amount of verification downstream undoes
having published the answer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator

from app.models.lostfound import ItemCategory, LostFoundKind, LostFoundStatus
from app.schemas.common import ApiModel


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _collapse(value: str) -> str:
    """Whitespace-collapse a required field.

    Matching tokenises the description, and " blue   bag " and "blue bag" must
    not score differently for the sake of how somebody's keyboard behaved.
    """
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ValueError("Describe the item in a few words")
    return collapsed


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
class LostItemCreate(ApiModel):
    """A pilgrim reporting something gone.

    `distinguishing_marks` is optional but heavily encouraged in the UI copy: it
    is what lets the desk hand the item back without a document, and a report
    without one can only ever be matched by a volunteer eyeballing a photo.
    """

    category: ItemCategory
    description: str = Field(min_length=2, max_length=500)
    colour: str | None = Field(default=None, max_length=24)
    distinguishing_marks: str | None = Field(default=None, max_length=500)
    zone_id: uuid.UUID | None = None
    #: When it was lost — not when the form was filled in. Defaults to now only
    #: because a frightened person should not be blocked by a date picker.
    occurred_at: datetime | None = None
    language: str = "mr"

    _strip_description = field_validator("description")(_collapse)

    @field_validator("colour", "distinguishing_marks")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        return value if value in {"mr", "hi", "en"} else "mr"


class FoundItemCreate(ApiModel):
    """A volunteer registering something handed in at a desk.

    `custody_facility_id` is required in spirit and validated in the route
    against the facility table: an item in the register with no desk against it
    is an item nobody can be sent to collect.
    """

    category: ItemCategory
    description: str = Field(min_length=2, max_length=500)
    colour: str | None = Field(default=None, max_length=24)
    distinguishing_marks: str | None = Field(default=None, max_length=500)
    zone_id: uuid.UUID | None = None
    custody_facility_id: uuid.UUID | None = None
    occurred_at: datetime | None = None
    photo_uri: str | None = Field(default=None, max_length=500)

    _strip_description = field_validator("description")(_collapse)

    @field_validator("colour", "distinguishing_marks")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return _clean(value)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
class LostFoundPublic(ApiModel):
    """Safe to show an unauthenticated or unrelated pilgrim.

    Note the absences: no `distinguishing_marks`, no `photo_uri`, no reporter,
    no exact time. `found_on` is a date, because the hour something was handed
    in is a detail only the person who lost it should be able to supply.
    """

    reference: str
    category: ItemCategory
    description: str
    colour: str | None = None
    zone_code: str | None = None
    zone_name_mr: str | None = None
    found_on: date
    custody_desk: str | None = None
    custody_desk_mr: str | None = None


class LostFoundMatchOut(ApiModel):
    id: uuid.UUID
    lost_item_id: uuid.UUID
    found_item_id: uuid.UUID
    score: float
    is_strong: bool
    #: The arithmetic, in pieces: same_zone, hours_apart, shared_words, colour.
    #: The desk reads these, not the number.
    reasons: dict[str, object] = Field(default_factory=dict)
    decision: str
    suggested_at: datetime
    decided_at: datetime | None = None
    #: The counterpart record, at public detail — a volunteer comparing two rows
    #: does not need the mark, and showing it here would put it on the screen of
    #: every desk in Pandharpur.
    counterpart: LostFoundPublic | None = None


class LostFoundOut(ApiModel):
    """Full detail. Staff, or the pilgrim who filed this record."""

    id: uuid.UUID
    reference: str
    kind: LostFoundKind
    category: ItemCategory
    description: str
    colour: str | None = None
    distinguishing_marks: str | None = None
    has_photo: bool = False
    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    custody_facility_id: uuid.UUID | None = None
    custody_desk: str | None = None
    status: LostFoundStatus
    matched_item_id: uuid.UUID | None = None
    occurred_at: datetime
    reported_at: datetime
    resolved_at: datetime | None = None
    purge_after: datetime | None = None
    language: str = "mr"

    claimed_by_name: str | None = None
    handed_over_at: datetime | None = None
    handover_note: str | None = None

    #: How long this has been open, so a desk can sort by "waiting longest"
    #: without doing date arithmetic in the browser.
    open_for_seconds: float = 0.0
    #: Filled on the single-record read only.
    suggestions: list[LostFoundMatchOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------
class MatchDecision(ApiModel):
    """Accept or reject a suggested pairing. A human, always."""

    found_item_id: uuid.UUID | None = None
    lost_item_id: uuid.UUID | None = None
    accept: bool = True
    note: str | None = Field(default=None, max_length=500)


class ClaimRequest(ApiModel):
    """A pilgrim saying "that one is mine, and here is how I know".

    `identifying_mark` is checked against what the finder wrote down. It is
    never echoed back, and a failure says only that it did not match — see
    `LOSTFOUND_CLAIM_UNVERIFIED`.
    """

    identifying_mark: str = Field(min_length=2, max_length=500)
    claimant_name: str = Field(min_length=1, max_length=120)


class HandoverRequest(ApiModel):
    """The desk recording that an object physically left with a person.

    `note` is mandatory and free text on purpose: the useful record after the
    fact is "matched the photo on her phone and the mark", written by the person
    who was standing there, not a checkbox.
    """

    claimant_name: str = Field(min_length=1, max_length=120)
    claimant_phone: str | None = Field(default=None, max_length=20)
    note: str = Field(min_length=3, max_length=1000)


class LostFoundUpdate(ApiModel):
    status: LostFoundStatus | None = None
    custody_facility_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=1000)
