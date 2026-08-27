"""Lost-and-found property: registration, matching, claim verification.

The interesting part of this module is what it refuses to do.

**Nothing is auto-matched.** `suggest` ranks candidate pairs and stores them;
`accept_match` requires a staff id. This mirrors `dispatch_service.suggest`, and
for a stronger reason: a wrong dispatch sends a volunteer to the wrong place,
while a wrong auto-match hands a stranger somebody's documents. There is no
score at which this module decides on its own — `SCORE_STRONG` changes how a
suggestion is *labelled*, never whether a human is asked.

**The claimant proves it, the desk does not tell them.** `verify_claim` takes
what the claimant says the distinguishing mark is and compares it to the stored
one. It returns a verdict, never the stored text. A volunteer holding a phone
cannot read the answer off their screen and then ask the question — the endpoint
that would let them do that does not exist.

**Matching uses when it was lost, not when it was reported.** A bag dropped at
the morning aarti is often reported at dusk. Scoring on report time suggests
items found hours before the loss, which is the single most common way a
lost-property system produces confident nonsense.

The scorer is deliberately arithmetic and readable rather than learned. Every
component is a fact a volunteer can check by looking at the two records, and
`reasons` carries them through to the UI so the desk sees "same category, same
zone, 40 minutes apart" and not "87%".
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import LostFoundItem, LostFoundMatch, LostFoundStatus, LostFoundKind, ItemCategory
from app.models.lostfound import OPEN_STATUSES, RETENTION_DAYS

logger = get_logger(__name__)

#: Same alphabet as incident references — no I, O, 0 or 1, because these get
#: read aloud across a help desk in a crowd.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: How far apart two records may be in time and still be considered the same
#: object. A day is generous on purpose: a pilgrim often does not notice until
#: they stop walking.
DEFAULT_WINDOW_HOURS = 36

#: Mobility aids get a much shorter window and a boost, because the useful
#: question for a lost walking stick is "was one handed in *this afternoon*" —
#: an owner who has been without it for a day has already had to solve the
#: problem another way (Track 1, item 4).
WALKING_AID_WINDOW_HOURS = 12

#: At or above this, the UI calls a suggestion "strong". It still needs a human.
SCORE_STRONG = 70.0
#: Below this a pair is not worth a volunteer's attention at a busy desk.
SCORE_FLOOR = 30.0

#: Suggestions returned per item. A desk that is shown forty candidates checks
#: none of them.
MAX_SUGGESTIONS = 8

_TOKEN_RE = re.compile(r"[^\wऀ-ॿ]+", re.UNICODE)

#: Words that carry no matching signal in either language. Without this, "bag"
#: in every bag description scores as agreement.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "my", "is", "was", "it", "of", "in", "at", "on", "and", "with",
        "colour", "color", "small", "big", "old", "new",
        "आहे", "होते", "माझा", "माझी", "माझे", "एक", "आणि", "मध्ये", "चा", "ची", "चे",
    }
)


def reference() -> str:
    return "LF-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def _tokens(*parts: str | None) -> set[str]:
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        for token in _TOKEN_RE.split(part.lower()):
            if len(token) > 2 and token not in _STOPWORDS:
                out.add(token)
    return out


def _normalise_mark(value: str) -> set[str]:
    return _tokens(value)


def window_hours(category: str) -> int:
    return WALKING_AID_WINDOW_HOURS if category == ItemCategory.WALKING_AID else DEFAULT_WINDOW_HOURS


@dataclass(slots=True)
class Candidate:
    """A scored pairing, with the arithmetic left visible."""

    item: LostFoundItem
    score: float
    reasons: dict[str, object] = field(default_factory=dict)

    @property
    def is_strong(self) -> bool:
        return self.score >= SCORE_STRONG


def score_pair(lost: LostFoundItem, found: LostFoundItem) -> Candidate | None:
    """Score a lost record against a found record.

    Returns `None` when the pair is impossible rather than merely unlikely —
    a found record that predates the loss, or a different category. Those are
    not low scores, they are different objects, and letting them through as
    a 20 would bury the real candidate at a busy desk.
    """
    if lost.category != found.category:
        return None

    # An item cannot be handed in before it is lost. One hour of slack absorbs
    # the pilgrim who reports "I lost it around noon" about a bag picked up at
    # 11:50 — human time estimates are not to the minute.
    gap = (found.occurred_at - lost.occurred_at).total_seconds()
    if gap < -3600:
        return None

    limit = window_hours(lost.category) * 3600
    if gap > limit:
        return None

    reasons: dict[str, object] = {"category": lost.category}

    # Category agreement is the floor, not a bonus — it is a precondition above.
    score = 40.0

    # Time proximity, decaying linearly across the window. Two records 30
    # minutes apart are far more interesting than two 30 hours apart.
    closeness = max(0.0, 1.0 - (max(0.0, gap) / limit))
    score += 25.0 * closeness
    reasons["hours_apart"] = round(max(0.0, gap) / 3600, 1)

    if lost.zone_id and found.zone_id:
        if lost.zone_id == found.zone_id:
            score += 20.0
            reasons["same_zone"] = True
        else:
            # Different known zones is weak evidence *against*: things move,
            # but usually not far, and a bag two zones away is more often a
            # different bag.
            score -= 10.0
            reasons["same_zone"] = False

    if lost.colour and found.colour and lost.colour.strip().lower() == found.colour.strip().lower():
        score += 10.0
        reasons["colour"] = lost.colour

    overlap = _tokens(lost.description) & _tokens(found.description)
    if overlap:
        # Capped: three shared words is not three times the evidence of one.
        score += min(10.0, 4.0 * len(overlap))
        reasons["shared_words"] = sorted(overlap)[:5]

    score = max(0.0, min(100.0, score))
    if score < SCORE_FLOOR:
        return None
    return Candidate(item=found, score=round(score, 1), reasons=reasons)


async def suggest(session: AsyncSession, item: LostFoundItem, *, limit: int = MAX_SUGGESTIONS) -> list[Candidate]:
    """Rank the open records on the other side of the desk against this one.

    Reads only open records: a returned bag is not a candidate for anything, and
    including closed rows is how a desk ends up re-matching an item that is
    already back with its owner.
    """
    opposite = LostFoundKind.FOUND if item.kind == LostFoundKind.LOST else LostFoundKind.LOST
    span = timedelta(hours=window_hours(item.category))

    stmt = (
        select(LostFoundItem)
        .where(
            LostFoundItem.kind == opposite,
            LostFoundItem.category == item.category,
            LostFoundItem.status.in_(OPEN_STATUSES),
            LostFoundItem.id != item.id,
            LostFoundItem.occurred_at >= item.occurred_at - span,
            LostFoundItem.occurred_at <= item.occurred_at + span,
        )
        .limit(200)
    )
    others = list((await session.execute(stmt)).scalars())

    candidates: list[Candidate] = []
    for other in others:
        lost, found = (item, other) if item.kind == LostFoundKind.LOST else (other, item)
        scored = score_pair(lost, found)
        if scored is None:
            continue
        # `score_pair` returns the found side; the caller wants the *other*
        # record whichever side it sat on.
        candidates.append(Candidate(item=other, score=scored.score, reasons=scored.reasons))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


async def persist_suggestions(
    session: AsyncSession, item: LostFoundItem, candidates: list[Candidate]
) -> list[LostFoundMatch]:
    """Store suggestions that are not already on file.

    Idempotent per pair — the unique constraint on (lost, found) is the real
    guard, and re-running the sweep must not create a second row or reopen a
    pair a volunteer already rejected.
    """
    stored: list[LostFoundMatch] = []
    for candidate in candidates:
        lost_id, found_id = (
            (item.id, candidate.item.id) if item.kind == LostFoundKind.LOST else (candidate.item.id, item.id)
        )
        existing = await session.scalar(
            select(LostFoundMatch).where(
                LostFoundMatch.lost_item_id == lost_id, LostFoundMatch.found_item_id == found_id
            )
        )
        if existing:
            continue
        match = LostFoundMatch(
            lost_item_id=lost_id,
            found_item_id=found_id,
            score=candidate.score,
            reasons=candidate.reasons,
            suggested_at=now_utc(),
            decision="pending",
        )
        session.add(match)
        stored.append(match)
    if stored:
        await session.flush()
    return stored


async def load(session: AsyncSession, item_id: uuid.UUID) -> LostFoundItem:
    record = await session.get(LostFoundItem, item_id)
    if record is None:
        raise AppError("LOSTFOUND_NOT_FOUND")
    return record


async def load_by_reference(session: AsyncSession, ref: str) -> LostFoundItem:
    record = await session.scalar(
        select(LostFoundItem).where(LostFoundItem.reference == ref.strip().upper())
    )
    if record is None:
        raise AppError("LOSTFOUND_NOT_FOUND")
    return record


def verify_claim(found: LostFoundItem, claimed_mark: str) -> tuple[bool, float]:
    """Does what the claimant remembers match what the finder wrote down?

    Returns `(passed, overlap_ratio)` and nothing else. In particular it does
    not return the stored mark, and no route exposes it — a volunteer who could
    read the answer would, under pressure, read it out.

    An item registered with no distinguishing mark cannot be verified this way
    at all, and says so rather than passing by default. The desk then falls back
    to a photo or to the handover note, both of which leave a record.
    """
    if not found.distinguishing_marks:
        return False, 0.0

    expected = _normalise_mark(found.distinguishing_marks)
    given = _normalise_mark(claimed_mark)
    if not expected or not given:
        return False, 0.0

    overlap = len(expected & given) / len(expected)
    # Half the remembered detail is a pass. Demanding more punishes the pilgrim
    # who says "tulsi mala inside" about a bag whose note reads "tulsi mala and
    # a steel tiffin", which is plainly the same bag.
    return overlap >= 0.5, round(overlap, 2)


def closure_purge_at(resolved_at=None):
    """Retention clock starts at closure, never at report."""
    return (resolved_at or now_utc()) + timedelta(days=RETENTION_DAYS)


async def open_counterparts(session: AsyncSession, phone_hash: str) -> list[LostFoundItem]:
    """Every open record a given pilgrim filed, newest first."""
    stmt = (
        select(LostFoundItem)
        .where(
            LostFoundItem.reporter_phone_hash == phone_hash,
            or_(
                LostFoundItem.status.in_(OPEN_STATUSES),
                LostFoundItem.status == LostFoundStatus.CLAIMED,
            ),
        )
        .order_by(LostFoundItem.reported_at.desc())
        .limit(50)
    )
    return list((await session.execute(stmt)).scalars())
