"""Lost and found property (Track 1, item 2).

    POST /lost-found/lost          a pilgrim reports something gone
    POST /lost-found/found         a volunteer registers something handed in
    GET  /lost-found/search        coarse public register — no marks, no photos
    GET  /lost-found/mine          the caller's own reports, full detail
    GET  /lost-found               the desk's working list
    GET  /lost-found/{id}          full detail + ranked suggestions
    POST /lost-found/{id}/match    a human accepts or rejects a pairing
    POST /lost-found/{id}/claim    a pilgrim proves an item is theirs
    POST /lost-found/{id}/handover the desk records that it physically left

`missing_persons` handles the half of lost-and-found that is a person. This is
the half that is property, and the rules differ in one decisive way: a person
is looking for you too, and an object is not. Everything below follows from
that — the register is searchable but coarse, matching is suggested but never
automatic, and nothing leaves the desk without two names against it.

Who may do what:

* **Reporting a loss is open to a pilgrim** (`lostfound:report`). Someone whose
  documents are gone should not have to find a volunteer to type for them.
* **Registering a found item is not** (`lostfound:manage`). Anyone may claim to
  have lost a bag; only a named volunteer may assert that one is in their hand.
  That asymmetry is the fraud model.
* **Reading the register in full is not** (`lostfound:view`). The public search
  is a separate, deliberately weaker endpoint.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, get_current_actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import hash_phone, now_utc
from app.models import Facility, LostFoundItem, LostFoundMatch, Zone
from app.models.lostfound import (
    OPEN_STATUSES,
    ItemCategory,
    LostFoundKind,
    LostFoundStatus,
)
from app.schemas.common import ErrorResponse, Page
from app.schemas.lostfound import (
    ClaimRequest,
    FoundItemCreate,
    HandoverRequest,
    LostFoundMatchOut,
    LostFoundOut,
    LostFoundPublic,
    LostFoundUpdate,
    LostItemCreate,
    MatchDecision,
)
from app.services import audit_service, lostfound_service
from app.services.audit_service import AuditAction
from app.services.lostfound_service import SCORE_STRONG

router = APIRouter(
    prefix="/lost-found",
    tags=["lost-found"],
    responses={404: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------
def _public(
    record: LostFoundItem,
    *,
    zone: Zone | None = None,
    desk: Facility | None = None,
) -> LostFoundPublic:
    """The coarse view. Every omission here is deliberate — see the schema."""
    return LostFoundPublic(
        reference=record.reference,
        category=ItemCategory(record.category),
        description=record.description,
        colour=record.colour,
        zone_code=zone.code if zone else None,
        zone_name_mr=zone.name_mr if zone else None,
        # A date, not a timestamp. The hour something was handed in is a detail
        # only its owner should be able to supply.
        found_on=record.occurred_at.date(),
        custody_desk=desk.name if desk else None,
        custody_desk_mr=desk.name_mr if desk else None,
    )


def _out(
    record: LostFoundItem,
    *,
    zone: Zone | None = None,
    desk: Facility | None = None,
    suggestions: list[LostFoundMatchOut] | None = None,
) -> LostFoundOut:
    end = record.resolved_at or now_utc()
    return LostFoundOut(
        id=record.id,
        reference=record.reference,
        kind=LostFoundKind(record.kind),
        category=ItemCategory(record.category),
        description=record.description,
        colour=record.colour,
        distinguishing_marks=record.distinguishing_marks,
        has_photo=bool(record.photo_uri),
        zone_id=record.zone_id,
        zone_code=zone.code if zone else None,
        custody_facility_id=record.custody_facility_id,
        custody_desk=desk.name if desk else None,
        status=LostFoundStatus(record.status),
        matched_item_id=record.matched_item_id,
        occurred_at=record.occurred_at,
        reported_at=record.reported_at,
        resolved_at=record.resolved_at,
        purge_after=record.purge_after,
        language=record.language,
        claimed_by_name=record.claimed_by_name,
        handed_over_at=record.handed_over_at,
        handover_note=record.handover_note,
        open_for_seconds=round((end - record.reported_at).total_seconds(), 1),
        suggestions=suggestions or [],
    )


async def _zone_of(session: AsyncSession, record: LostFoundItem) -> Zone | None:
    return await session.get(Zone, record.zone_id) if record.zone_id else None


async def _desk_of(session: AsyncSession, record: LostFoundItem) -> Facility | None:
    return await session.get(Facility, record.custody_facility_id) if record.custody_facility_id else None


def _assert_open(record: LostFoundItem) -> None:
    if record.status not in OPEN_STATUSES:
        raise AppError("LOSTFOUND_CLOSED", details={"status": record.status})


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
@router.post("/lost", response_model=LostFoundOut, status_code=201)
async def report_lost_item(
    payload: LostItemCreate,
    actor: Actor = Depends(require(Permission.LOSTFOUND_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """File a loss, and get back whatever the register already holds.

    The suggestions are computed and returned on the spot rather than in a later
    sweep, because the moment a pilgrim finishes describing their bag is the
    moment they are standing in front of somebody who can walk them to a desk.
    A match found four hours later, when they are on a bus home, is worth much
    less.
    """
    record = LostFoundItem(
        reference=lostfound_service.reference(),
        kind=LostFoundKind.LOST,
        category=payload.category,
        description=payload.description,
        colour=payload.colour,
        distinguishing_marks=payload.distinguishing_marks,
        zone_id=payload.zone_id,
        occurred_at=payload.occurred_at or now_utc(),
        reported_by=actor.id,
        reporter_phone_hash=actor.user.phone_hash,
        language=payload.language,
        status=LostFoundStatus.OPEN,
        reported_at=now_utc(),
    )
    session.add(record)
    await session.flush()

    candidates = await lostfound_service.suggest(session, record)
    await lostfound_service.persist_suggestions(session, record, candidates)

    await audit_service.record(
        session,
        action=AuditAction.LOSTFOUND_REPORTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="lost_found_item",
        target_id=record.id,
        meta={"kind": "lost", "category": record.category, "suggestions": len(candidates)},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    suggestions = await _suggestions_for(session, record)
    out = _out(record, zone=await _zone_of(session, record), suggestions=suggestions)
    await session.commit()
    return out


@router.post("/found", response_model=LostFoundOut, status_code=201)
async def register_found_item(
    payload: FoundItemCreate,
    actor: Actor = Depends(require(Permission.LOSTFOUND_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """Register something handed in at a desk.

    The custody facility is validated rather than trusted: an item filed against
    a desk that does not exist is an item a pilgrim will be sent to collect from
    nowhere.
    """
    desk: Facility | None = None
    if payload.custody_facility_id:
        desk = await session.get(Facility, payload.custody_facility_id)
        if desk is None:
            raise AppError("NOT_FOUND", details={"facility_id": str(payload.custody_facility_id)})

    record = LostFoundItem(
        reference=lostfound_service.reference(),
        kind=LostFoundKind.FOUND,
        category=payload.category,
        description=payload.description,
        colour=payload.colour,
        distinguishing_marks=payload.distinguishing_marks,
        photo_uri=payload.photo_uri,
        zone_id=payload.zone_id,
        custody_facility_id=payload.custody_facility_id,
        occurred_at=payload.occurred_at or now_utc(),
        reported_by=actor.id,
        status=LostFoundStatus.OPEN,
        reported_at=now_utc(),
    )
    session.add(record)
    await session.flush()

    candidates = await lostfound_service.suggest(session, record)
    await lostfound_service.persist_suggestions(session, record, candidates)

    await audit_service.record(
        session,
        action=AuditAction.LOSTFOUND_REPORTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="lost_found_item",
        target_id=record.id,
        meta={"kind": "found", "category": record.category, "suggestions": len(candidates)},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    suggestions = await _suggestions_for(session, record)
    out = _out(record, zone=await _zone_of(session, record), desk=desk, suggestions=suggestions)
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@router.get("/search", response_model=Page[LostFoundPublic])
async def search_found_register(
    category: ItemCategory | None = None,
    zone_id: uuid.UUID | None = None,
    days: int = Query(default=3, ge=1, le=30, description="How far back to look"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.LOSTFOUND_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> Page[LostFoundPublic]:
    """The pilgrim-facing register of things handed in.

    Coarse by construction — `LostFoundPublic` carries no identifying mark, no
    photo and no exact time. This endpoint exists to get somebody to walk to the
    right desk, not to let them describe an item they have never seen.

    Only `found` records, and only open ones. A returned item is nobody's
    business but its owner's.
    """
    from datetime import timedelta

    since = now_utc() - timedelta(days=days)
    stmt = select(LostFoundItem).where(
        LostFoundItem.kind == LostFoundKind.FOUND,
        LostFoundItem.status.in_(OPEN_STATUSES),
        LostFoundItem.occurred_at >= since,
    )
    if category:
        stmt = stmt.where(LostFoundItem.category == category)
    if zone_id:
        stmt = stmt.where(LostFoundItem.zone_id == zone_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(LostFoundItem.occurred_at.desc()).limit(limit).offset(offset)
    )
    records = list(rows.scalars())

    zones = await _zone_map(session, {r.zone_id for r in records if r.zone_id})
    desks = await _desk_map(session, {r.custody_facility_id for r in records if r.custody_facility_id})

    return Page[LostFoundPublic](
        items=[
            _public(
                r,
                zone=zones.get(r.zone_id) if r.zone_id else None,
                desk=desks.get(r.custody_facility_id) if r.custody_facility_id else None,
            )
            for r in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/mine", response_model=list[LostFoundOut])
async def my_reports(
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> list[LostFoundOut]:
    """The caller's own records, in full, including their suggested matches.

    Full detail is right here and only here: these are the pilgrim's own reports,
    so the identifying mark on them is the one they wrote themselves.
    """
    records = await lostfound_service.open_counterparts(session, actor.user.phone_hash)
    zones = await _zone_map(session, {r.zone_id for r in records if r.zone_id})
    out: list[LostFoundOut] = []
    for record in records:
        out.append(
            _out(
                record,
                zone=zones.get(record.zone_id) if record.zone_id else None,
                suggestions=await _suggestions_for(session, record),
            )
        )
    return out


@router.get("", response_model=Page[LostFoundOut])
async def list_records(
    kind: LostFoundKind | None = None,
    category: ItemCategory | None = None,
    status: LostFoundStatus | None = None,
    open_only: bool = Query(default=True),
    zone_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.LOSTFOUND_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[LostFoundOut]:
    """The desk's working list.

    Oldest-first, like the missing-persons list and for the same reason: these
    records carry no severity, so elapsed time is the only thing that ranks
    them. The bag that has been sitting on the shelf longest is the one whose
    owner is furthest away by now.
    """
    stmt = select(LostFoundItem)
    if kind:
        stmt = stmt.where(LostFoundItem.kind == kind)
    if category:
        stmt = stmt.where(LostFoundItem.category == category)
    if status:
        stmt = stmt.where(LostFoundItem.status == status)
    elif open_only:
        stmt = stmt.where(LostFoundItem.status.in_(OPEN_STATUSES))
    if zone_id:
        stmt = stmt.where(LostFoundItem.zone_id == zone_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(LostFoundItem.reported_at.asc()).limit(limit).offset(offset)
    )
    records = list(rows.scalars())

    zones = await _zone_map(session, {r.zone_id for r in records if r.zone_id})
    desks = await _desk_map(session, {r.custody_facility_id for r in records if r.custody_facility_id})

    return Page[LostFoundOut](
        items=[
            _out(
                r,
                zone=zones.get(r.zone_id) if r.zone_id else None,
                desk=desks.get(r.custody_facility_id) if r.custody_facility_id else None,
            )
            for r in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{item_id}", response_model=LostFoundOut)
async def get_record(
    item_id: uuid.UUID,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """Full detail, for staff or for the person who filed it.

    The ownership check is the same shape as the one on a pass: a pilgrim may
    read their own record, and reading anybody else's needs the desk permission.
    """
    record = await lostfound_service.load(session, item_id)
    mine = record.reporter_phone_hash and record.reporter_phone_hash == actor.user.phone_hash
    if not mine and not actor.can(Permission.LOSTFOUND_VIEW):
        raise AppError("FORBIDDEN")

    return _out(
        record,
        zone=await _zone_of(session, record),
        desk=await _desk_of(session, record),
        suggestions=await _suggestions_for(session, record),
    )


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------
@router.post("/{item_id}/match", response_model=LostFoundOut)
async def decide_match(
    item_id: uuid.UUID,
    payload: MatchDecision,
    actor: Actor = Depends(require(Permission.LOSTFOUND_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """Accept or reject a suggested pairing.

    A human, always. There is no score at which this happens by itself — see the
    module docstring on `lostfound_service`. Accepting links both rows to each
    other and moves both to `matched`; it does *not* mark anything returned,
    because the object is still on the shelf until somebody carries it away.

    A rejection is kept rather than deleted. It is the only evidence that the
    scorer is wrong in a particular way, and the only input a future change to
    the weights could be checked against.
    """
    record = await lostfound_service.load(session, item_id)
    _assert_open(record)

    counterpart_id = payload.found_item_id or payload.lost_item_id
    if counterpart_id is None:
        raise AppError("VALIDATION_ERROR", details={"reason": "give the id of the record to pair with"})
    counterpart = await lostfound_service.load(session, counterpart_id)
    if counterpart.kind == record.kind:
        raise AppError("LOSTFOUND_KIND_MISMATCH", details={"both": record.kind})
    _assert_open(counterpart)

    lost, found = (record, counterpart) if record.kind == LostFoundKind.LOST else (counterpart, record)

    match = await session.scalar(
        select(LostFoundMatch).where(
            LostFoundMatch.lost_item_id == lost.id, LostFoundMatch.found_item_id == found.id
        )
    )
    if match is None:
        # A volunteer who has both objects in front of them beats the scorer.
        # Recording the pairing they made by eye matters more than insisting it
        # was suggested first.
        scored = lostfound_service.score_pair(lost, found)
        match = LostFoundMatch(
            lost_item_id=lost.id,
            found_item_id=found.id,
            score=scored.score if scored else 0.0,
            reasons=(scored.reasons if scored else {"manual": True}),
            suggested_at=now_utc(),
            decision="pending",
        )
        session.add(match)
        await session.flush()

    match.decision = "accepted" if payload.accept else "rejected"
    match.decided_by = actor.id
    match.decided_at = now_utc()

    if payload.accept:
        lost.matched_item_id = found.id
        found.matched_item_id = lost.id
        lost.status = LostFoundStatus.MATCHED
        found.status = LostFoundStatus.MATCHED

    await audit_service.record(
        session,
        action=AuditAction.LOSTFOUND_MATCHED if payload.accept else AuditAction.LOSTFOUND_MATCH_REJECTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="lost_found_item",
        target_id=record.id,
        meta={
            "lost": str(lost.id),
            "found": str(found.id),
            "score": match.score,
            "note": payload.note,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    out = _out(
        record,
        zone=await _zone_of(session, record),
        desk=await _desk_of(session, record),
        suggestions=await _suggestions_for(session, record),
    )
    await session.commit()
    return out


@router.post("/{item_id}/claim", response_model=LostFoundOut)
async def claim_item(
    item_id: uuid.UUID,
    payload: ClaimRequest,
    actor: Actor = Depends(require(Permission.LOSTFOUND_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """A pilgrim asserting a found item is theirs, and proving it.

    The proof is the identifying mark the finder wrote down. `verify_claim`
    compares and returns a verdict; the stored text is never sent anywhere, and
    a failure says only that it did not match — telling a guesser how close they
    got turns this into an oracle.

    A pass here does *not* hand the object over. It moves the record to
    `claimed`, which is the state that means "somebody has established a right
    to this, and it is still on the shelf". Only `/handover` empties the shelf,
    and only a volunteer can call it.
    """
    record = await lostfound_service.load(session, item_id)
    if record.kind != LostFoundKind.FOUND:
        raise AppError("LOSTFOUND_KIND_MISMATCH", details={"kind": record.kind})
    _assert_open(record)

    if not record.distinguishing_marks:
        raise AppError("LOSTFOUND_NOT_VERIFIABLE", details={"reference": record.reference})

    passed, overlap = lostfound_service.verify_claim(record, payload.identifying_mark)
    if not passed:
        # Audited even on failure. A pattern of failed claims against different
        # items by one account is the signal this whole design exists to catch.
        await audit_service.record(
            session,
            action=AuditAction.LOSTFOUND_MATCH_REJECTED,
            actor_id=actor.id,
            actor_role=actor.user.role,
            target_type="lost_found_item",
            target_id=record.id,
            meta={"claim_failed": True, "overlap": overlap},
            ip=actor.ip,
            user_agent=actor.user_agent,
        )
        await session.commit()
        raise AppError("LOSTFOUND_CLAIM_UNVERIFIED")

    record.status = LostFoundStatus.CLAIMED
    record.claimed_by_name = payload.claimant_name
    record.claimed_by_phone_hash = actor.user.phone_hash

    desk = await _desk_of(session, record)
    out = _out(record, zone=await _zone_of(session, record), desk=desk)
    await session.commit()
    return out


@router.post("/{item_id}/handover", response_model=LostFoundOut)
async def hand_over(
    item_id: uuid.UUID,
    payload: HandoverRequest,
    actor: Actor = Depends(require(Permission.LOSTFOUND_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """Record that an object physically left the desk with a person.

    Two names against it, always: who took it, and which volunteer watched them
    take it. An item that left with only one of those recorded is
    indistinguishable afterwards from an item stolen off the shelf, which is why
    `note` is mandatory and free text — the useful record is a sentence written
    by the person standing there.

    This is where the retention clock starts. `purge_after` is set from closure,
    never from the report, so a bag that sat unclaimed for three weeks keeps its
    photo for thirty days *after* it is finally dealt with.
    """
    record = await lostfound_service.load(session, item_id)
    if record.status in (LostFoundStatus.RETURNED, LostFoundStatus.EXPIRED):
        raise AppError("LOSTFOUND_CLOSED", details={"status": record.status})

    record.status = LostFoundStatus.RETURNED
    record.claimed_by_name = payload.claimant_name
    if payload.claimant_phone:
        record.claimed_by_phone_hash = hash_phone(payload.claimant_phone)
    record.handed_over_by = actor.id
    record.handed_over_at = now_utc()
    record.handover_note = payload.note
    record.resolved_at = now_utc()
    record.purge_after = lostfound_service.closure_purge_at(record.resolved_at)

    # The matching lost report closes with it. Leaving it open would keep the
    # owner's own record showing "still looking" after they have the bag.
    if record.matched_item_id:
        counterpart = await session.get(LostFoundItem, record.matched_item_id)
        if counterpart and counterpart.status in OPEN_STATUSES + (LostFoundStatus.CLAIMED,):
            counterpart.status = LostFoundStatus.RETURNED
            counterpart.resolved_at = record.resolved_at
            counterpart.purge_after = record.purge_after

    await audit_service.record(
        session,
        action=AuditAction.LOSTFOUND_HANDED_OVER,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="lost_found_item",
        target_id=record.id,
        meta={
            "reference": record.reference,
            "claimant": payload.claimant_name,
            "matched_item": str(record.matched_item_id) if record.matched_item_id else None,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    out = _out(record, zone=await _zone_of(session, record), desk=await _desk_of(session, record))
    await session.commit()
    return out


@router.patch("/{item_id}", response_model=LostFoundOut)
async def update_record(
    item_id: uuid.UUID,
    payload: LostFoundUpdate,
    actor: Actor = Depends(require(Permission.LOSTFOUND_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> LostFoundOut:
    """Move a record's status, or correct which desk is holding it.

    `returned` is not reachable here — that transition needs a claimant name and
    a note, so it lives on `/handover` where those are mandatory.
    """
    record = await lostfound_service.load(session, item_id)

    if payload.status:
        if payload.status == LostFoundStatus.RETURNED:
            raise AppError(
                "VALIDATION_ERROR",
                details={"reason": "use /handover — a return needs a claimant and a note"},
            )
        record.status = payload.status
        if payload.status in (LostFoundStatus.CLOSED_UNRESOLVED, LostFoundStatus.EXPIRED):
            record.resolved_at = now_utc()
            record.purge_after = lostfound_service.closure_purge_at(record.resolved_at)

    if payload.custody_facility_id is not None:
        desk = await session.get(Facility, payload.custody_facility_id)
        if desk is None:
            raise AppError("NOT_FOUND", details={"facility_id": str(payload.custody_facility_id)})
        record.custody_facility_id = payload.custody_facility_id

    out = _out(record, zone=await _zone_of(session, record), desk=await _desk_of(session, record))
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _zone_map(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, Zone]:
    if not ids:
        return {}
    rows = await session.execute(select(Zone).where(Zone.id.in_(ids)))
    return {z.id: z for z in rows.scalars()}


async def _desk_map(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, Facility]:
    if not ids:
        return {}
    rows = await session.execute(select(Facility).where(Facility.id.in_(ids)))
    return {f.id: f for f in rows.scalars()}


async def _suggestions_for(session: AsyncSession, record: LostFoundItem) -> list[LostFoundMatchOut]:
    """Stored suggestions for a record, with the counterpart at public detail.

    Public detail on purpose: a volunteer comparing two rows does not need the
    identifying mark to decide whether a blue bag found at Gate 3 might be the
    blue bag lost at Gate 3, and putting it here would place it on every desk
    screen in Pandharpur.
    """
    side = (
        LostFoundMatch.lost_item_id if record.kind == LostFoundKind.LOST else LostFoundMatch.found_item_id
    )
    rows = await session.execute(
        select(LostFoundMatch).where(side == record.id).order_by(LostFoundMatch.score.desc()).limit(10)
    )
    matches = list(rows.scalars())
    if not matches:
        return []

    other_ids = {
        (m.found_item_id if record.kind == LostFoundKind.LOST else m.lost_item_id) for m in matches
    }
    others = await session.execute(select(LostFoundItem).where(LostFoundItem.id.in_(other_ids)))
    by_id = {i.id: i for i in others.scalars()}

    zones = await _zone_map(session, {i.zone_id for i in by_id.values() if i.zone_id})
    desks = await _desk_map(
        session, {i.custody_facility_id for i in by_id.values() if i.custody_facility_id}
    )

    out: list[LostFoundMatchOut] = []
    for match in matches:
        other_id = match.found_item_id if record.kind == LostFoundKind.LOST else match.lost_item_id
        other = by_id.get(other_id)
        out.append(
            LostFoundMatchOut(
                id=match.id,
                lost_item_id=match.lost_item_id,
                found_item_id=match.found_item_id,
                score=match.score,
                is_strong=match.score >= SCORE_STRONG,
                reasons=match.reasons or {},
                decision=match.decision,
                suggested_at=match.suggested_at,
                decided_at=match.decided_at,
                counterpart=(
                    _public(
                        other,
                        zone=zones.get(other.zone_id) if other.zone_id else None,
                        desk=(
                            desks.get(other.custody_facility_id) if other.custody_facility_id else None
                        ),
                    )
                    if other
                    else None
                ),
            )
        )
    return out
