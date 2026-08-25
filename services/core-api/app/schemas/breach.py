"""Breach ledger schemas (Section 4/M5).

The shape of these models is a privacy decision before it is an API decision.

There is no field anywhere in this file that identifies a person, and there is
nowhere one could be added without it being obvious in review. `BreachOut`
describes a gate, a time, a direction and a confidence. That is the whole claim
the system makes: *an unauthorised entry occurred at Gate 3 at 14:22*. Who did
it is a lawful human process, not an inference this product offers.

Two further rules the shapes enforce:

* **`clip_uri` is never on a list response.** It appears only in the reply to a
  re-authenticated `POST /breaches/{id}/clip`, which logs the view. A URI on a
  list endpoint is a URI in a browser history, a screenshot and a support ticket.
* **`review_status` is always present and starts at `pending`.** There is no
  representation of an event that has been "confirmed by the system". Section
  4/M5: AI output alone is never a finding.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.models.breach import ReviewStatus
from app.schemas.common import ApiModel


# ---------------------------------------------------------------------------
# ingest (AI engine -> core API)
# ---------------------------------------------------------------------------
class CrossingIn(ApiModel):
    """One tripwire crossing, as reported by the vision pipeline.

    Note what the engine is *not* asked for and has nowhere to send: a track id,
    a bounding box, an appearance descriptor, a face. The crossing is a count
    and a direction. Track ids are ephemeral and in-memory on the engine side
    (Section 12) and they stop existing at this boundary.
    """

    tripwire_id: uuid.UUID
    occurred_at: datetime
    direction: Literal["in", "out"]
    confidence: float = Field(ge=0, le=1)
    crossing_count: int = Field(default=1, ge=1, le=100)
    #: Where the 10-second clip (5s pre-roll, 5s post) was written.
    clip_uri: str | None = Field(default=None, max_length=500)
    #: SHA-256 of the clip, computed by whoever wrote it. Hashed on write so the
    #: chain commits to the evidence, not merely to a filename that could later
    #: point somewhere else.
    clip_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class CrossingBatch(ApiModel):
    crossings: list[CrossingIn] = Field(min_length=1, max_length=100)


class CrossingResult(ApiModel):
    """What became of each reported crossing.

    Rejections are returned rather than swallowed so the engine's logs can be
    reconciled against the ledger. "The engine saw 40 crossings and the ledger
    has 3" is a question somebody will ask, and the 37 reasons are the answer.
    """

    recorded: int
    ignored: int
    reasons: dict[str, int] = Field(default_factory=dict)
    sequences: list[int] = Field(default_factory=list)
    received_at: datetime


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
class BreachOut(ApiModel):
    id: uuid.UUID
    #: Position in the hash chain. Gaps are themselves evidence.
    sequence: int
    tripwire_id: uuid.UUID
    tripwire_name: str | None = None
    camera_id: uuid.UUID
    gate_id: uuid.UUID | None = None
    gate_code: str | None = None
    gate_name_mr: str | None = None
    occurred_at: datetime
    direction: str
    crossing_count: int
    confidence: float

    #: Whether a clip exists — never where it is. Fetching it is a separate,
    #: re-authenticated, logged act.
    has_clip: bool = False
    clip_sha256: str | None = None

    #: The cross-reference that was made, and its outcome. "We checked and found
    #: no pass" and "nobody checked" are different claims and a reviewer needs
    #: to know which this was.
    pass_scan_checked: bool

    review_status: ReviewStatus
    reviewed_by: uuid.UUID | None = None
    review_reason: str | None = None
    reviewed_at: datetime | None = None

    #: Set when evidence was removed. The record itself never disappears — see
    #: `breach_service.redact`.
    redacted_at: datetime | None = None
    redaction_reason: str | None = None

    chain_hash: str
    prev_hash: str
    purge_after: datetime
    created_at: datetime

    #: Only on the single-record read.
    clip_views: list[ClipViewOut] = Field(default_factory=list)


class ClipViewOut(ApiModel):
    """One line of the clip's access trail."""

    actor_id: uuid.UUID
    purpose: str
    ip: str | None = None
    accessed_at: datetime


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
class ReviewIn(ApiModel):
    """A human's decision.

    `pending` is not an option. A review is a decision, and offering a way back
    to "undecided" would erase the fact that somebody looked.
    """

    status: Literal["verified", "false_positive", "authorised"]
    #: Mandatory for `authorised` and `false_positive` — enforced in the
    #: service, because the rule is about the transition rather than the payload.
    reason: str | None = Field(default=None, max_length=1000)


class ClipRequest(ApiModel):
    """Re-authentication plus a stated purpose (Section 4/M5).

    Both are required. The password proves the person at the keyboard is still
    the person who signed in; the purpose is what an inquiry reads. A viewing
    with no stated reason is the one that gets asked about, and requiring the
    field means the answer exists before the question does.
    """

    password: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=3, max_length=500)


class ClipOut(ApiModel):
    breach_id: uuid.UUID
    sequence: int
    clip_uri: str
    clip_sha256: str | None = None
    #: Restated in the response so the caller cannot claim not to have known.
    notice: str
    notice_mr: str
    logged_at: datetime


class RedactIn(ApiModel):
    """Removing evidence. System Admin only, reason mandatory, logged forever."""

    reason: str = Field(min_length=10, max_length=1000)


# ---------------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------------
class ChainBreakOut(ApiModel):
    sequence: int
    breach_id: uuid.UUID | None = None
    problem: str
    expected: str | None = None
    found: str | None = None


class ChainReportOut(ApiModel):
    events_checked: int
    intact: bool
    breaks: list[ChainBreakOut] = Field(default_factory=list)
    first_sequence: int | None = None
    last_sequence: int | None = None
    #: The hash of the newest record. Worth writing down somewhere outside this
    #: database — an attacker who can rewrite the ledger can also rewrite a
    #: verification that only ever compares the ledger to itself.
    head_hash: str | None = None
    verified_at: datetime
    note: str
    note_mr: str


# ---------------------------------------------------------------------------
# governance report
# ---------------------------------------------------------------------------
class GateHourOut(ApiModel):
    gate_id: uuid.UUID | None = None
    gate_code: str | None = None
    hour: int
    count: int


class DailySummaryOut(ApiModel):
    """The artefact the trust takes to a governance meeting.

    Counts by gate and hour, and by review status. No personal data, because
    there is none in the table — the report is safe to circulate by
    construction rather than by redaction.
    """

    day: date
    total: int
    by_review_status: dict[str, int] = Field(default_factory=dict)
    by_gate_hour: list[GateHourOut] = Field(default_factory=list)
    #: Whether the ledger this report came from verifies. A breach report that
    #: does not say this invites the question at the worst possible moment.
    chain_intact: bool
    chain_head: str | None = None
    generated_at: datetime
    notice: str
    notice_mr: str


# ---------------------------------------------------------------------------
# tripwires
# ---------------------------------------------------------------------------
class TripwireIn(ApiModel):
    """A line drawn on a camera frame at a restricted gate."""

    camera_id: uuid.UUID
    gate_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    #: Two points in normalised image coordinates (0..1), so the line survives a
    #: change of stream resolution.
    points: list[tuple[float, float]] = Field(min_length=2, max_length=2)
    restricted_direction: Literal["in", "out"]
    active_schedule: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class TripwireOut(ApiModel):
    id: uuid.UUID
    camera_id: uuid.UUID
    camera_name: str | None = None
    gate_id: uuid.UUID | None = None
    gate_code: str | None = None
    name: str
    points: list[tuple[float, float]]
    restricted_direction: str
    active_schedule: dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    #: How many events this line has produced, and how many are still unreviewed.
    #: A tripwire generating hundreds of pending events is mis-drawn, and this is
    #: where that becomes visible.
    event_count: int = 0
    pending_count: int = 0
