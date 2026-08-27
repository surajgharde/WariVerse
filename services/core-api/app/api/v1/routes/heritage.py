"""The Wari heritage archive (Track 1, item 5).

    GET   /heritage                published records, for anyone
    GET   /heritage/{id}           one record
    POST  /heritage                a pilgrim contributes — always `pending`
    GET   /heritage/mine           what the caller submitted, and its fate
    GET   /heritage/review/queue   the moderation queue
    POST  /heritage/{id}/review    publish it, or decline it and say why
    PATCH /heritage/{id}           a moderator's correction

The moderation gate is the feature, not an obstacle to it. An archive of
religious tradition that anybody can write into is a vandalism target and, worse,
a place where a plausible invention quietly becomes a citation somebody repeats.

The public read is anonymous on purpose. A Warkari standing at a halt town
should be able to read why the Palkhi stops there without signing in, and an
archive is not worth building if it is only legible to people with accounts.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, get_current_actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import HeritageItem
from app.models.heritage import PUBLIC_STATES, HeritageKind, ReviewState
from app.schemas.common import ErrorResponse, Page
from app.schemas.heritage import (
    HeritageContribute,
    HeritageOut,
    HeritagePublic,
    HeritageReview,
    HeritageUpdate,
)
from app.services import audit_service
from app.services.audit_service import AuditAction

router = APIRouter(prefix="/heritage", tags=["heritage"], responses={404: {"model": ErrorResponse}})


def _public(record: HeritageItem) -> HeritagePublic:
    return HeritagePublic(
        id=record.id,
        kind=HeritageKind(record.kind),
        title_mr=record.title_mr,
        title_en=record.title_en,
        body_mr=record.body_mr,
        body_en=record.body_en,
        attribution=record.attribution,
        source=record.source,
        era=record.era,
        media_uri=record.media_uri,
        media_type=record.media_type,
        halt_town_id=record.halt_town_id,
        dindi_id=record.dindi_id,
        tags=list(record.tags or []),
        contributed_by_name=record.contributed_by_name,
        published_at=record.published_at,
    )


def _out(record: HeritageItem) -> HeritageOut:
    return HeritageOut(
        **_public(record).model_dump(),
        status=ReviewState(record.status),
        reviewed_at=record.reviewed_at,
        review_note=record.review_note,
        created_at=record.created_at,
    )


async def _load(session: AsyncSession, item_id: uuid.UUID) -> HeritageItem:
    record = await session.get(HeritageItem, item_id)
    if record is None:
        raise AppError("HERITAGE_NOT_FOUND")
    return record


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@router.get("", response_model=Page[HeritagePublic])
async def list_published(
    kind: HeritageKind | None = None,
    halt_town_id: uuid.UUID | None = None,
    dindi_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=100, description="Match title or attribution"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> Page[HeritagePublic]:
    """Published records, newest first, no sign-in required.

    Anonymous deliberately: a Warkari standing at a halt town should be able to
    read why the Palkhi stops there without an account, and the whole archive is
    material somebody has already decided is fit to publish.

    Ordered newest-published rather than by relevance. There is no ranking
    signal here worth the name, and a stable order is what lets the pilgrim app
    page through it and cache the result for offline reading.
    """
    stmt = select(HeritageItem).where(HeritageItem.status.in_(PUBLIC_STATES))
    if kind:
        stmt = stmt.where(HeritageItem.kind == kind)
    if halt_town_id:
        stmt = stmt.where(HeritageItem.halt_town_id == halt_town_id)
    if dindi_id:
        stmt = stmt.where(HeritageItem.dindi_id == dindi_id)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                HeritageItem.title_mr.ilike(pattern),
                HeritageItem.title_en.ilike(pattern),
                HeritageItem.attribution.ilike(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(HeritageItem.published_at.desc().nullslast()).limit(limit).offset(offset)
    )
    return Page[HeritagePublic](
        items=[_public(r) for r in rows.scalars()], total=total, limit=limit, offset=offset
    )


@router.get("/mine", response_model=list[HeritageOut])
async def my_contributions(
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> list[HeritageOut]:
    """What the caller submitted, and what became of it.

    A contributor is entitled to know their grandmother's ovi was declined and
    why. Nobody else is, which is why this is keyed on the caller and not a
    filter on the moderation queue.
    """
    rows = await session.execute(
        select(HeritageItem)
        .where(HeritageItem.contributed_by == actor.id)
        .order_by(HeritageItem.created_at.desc())
        .limit(100)
    )
    return [_out(r) for r in rows.scalars()]


@router.get("/review/queue", response_model=Page[HeritageOut])
async def review_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.HERITAGE_MODERATE)),
    session: AsyncSession = Depends(get_session),
) -> Page[HeritageOut]:
    """Oldest first. A contribution that has waited a week is the one to read."""
    stmt = select(HeritageItem).where(HeritageItem.status == ReviewState.PENDING)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(HeritageItem.created_at.asc()).limit(limit).offset(offset)
    )
    return Page[HeritageOut](
        items=[_out(r) for r in rows.scalars()], total=total, limit=limit, offset=offset
    )


@router.get("/{item_id}", response_model=HeritageOut)
async def get_item(
    item_id: uuid.UUID,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> HeritageOut:
    """One record.

    Published records are readable by any signed-in caller. An unpublished one
    is visible to its contributor and to a moderator — and to nobody else, so a
    rejected submission cannot be fished out by walking ids.
    """
    record = await _load(session, item_id)
    if record.status not in PUBLIC_STATES:
        mine = record.contributed_by == actor.id
        if not mine and not actor.can(Permission.HERITAGE_MODERATE):
            # 404, not 403: confirming that an id exists but is hidden is itself
            # the answer somebody walking ids is looking for.
            raise AppError("HERITAGE_NOT_FOUND")
    return _out(record)


# ---------------------------------------------------------------------------
# contributing
# ---------------------------------------------------------------------------
@router.post("", response_model=HeritageOut, status_code=201)
async def contribute(
    payload: HeritageContribute,
    actor: Actor = Depends(require(Permission.HERITAGE_CONTRIBUTE)),
    session: AsyncSession = Depends(get_session),
) -> HeritageOut:
    """Submit something for the archive.

    Always `pending`, whatever the caller sends: the status is set here and the
    schema has no field for it. A moderator publishes, or declines with a reason
    that stays attached to the text.
    """
    record = HeritageItem(
        kind=payload.kind,
        title_mr=payload.title_mr,
        title_en=payload.title_en,
        body_mr=payload.body_mr,
        body_en=payload.body_en,
        attribution=payload.attribution,
        source=payload.source,
        era=payload.era,
        media_uri=payload.media_uri,
        media_type=payload.media_type,
        halt_town_id=payload.halt_town_id,
        dindi_id=payload.dindi_id,
        tags=payload.tags,
        contributed_by=actor.id,
        contributed_by_name=payload.contributed_by_name or actor.user.name,
        status=ReviewState.PENDING,
    )
    session.add(record)
    await session.flush()
    await session.refresh(record)

    await audit_service.record(
        session,
        action=AuditAction.HERITAGE_CONTRIBUTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="heritage_item",
        target_id=record.id,
        meta={"kind": record.kind},
        ip=actor.ip,
    )
    out = _out(record)
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# moderation
# ---------------------------------------------------------------------------
@router.post("/{item_id}/review", response_model=HeritageOut)
async def review_item(
    item_id: uuid.UUID,
    payload: HeritageReview,
    actor: Actor = Depends(require(Permission.HERITAGE_MODERATE)),
    session: AsyncSession = Depends(get_session),
) -> HeritageOut:
    """Publish a contribution, or decline it with a reason.

    Declining requires a note. A rejection with no reason is unappealable and
    unlearnable — the contributor cannot fix it and the next moderator cannot
    tell whether the same decision would be made again.

    The text is never deleted. Someone's grandmother's version of an ovi being
    wrong for this archive is not a reason to destroy the only copy anybody has
    typed out.
    """
    record = await _load(session, item_id)

    if not payload.publish and not (payload.note or "").strip():
        raise AppError(
            "REVIEW_NOTE_REQUIRED",
            details={"reason": "declining a contribution requires a reason the contributor can read"},
        )

    record.status = ReviewState.PUBLISHED if payload.publish else ReviewState.REJECTED
    record.reviewed_by = actor.id
    record.reviewed_at = now_utc()
    record.review_note = payload.note
    record.published_at = now_utc() if payload.publish else None

    await audit_service.record(
        session,
        action=AuditAction.HERITAGE_REVIEWED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="heritage_item",
        target_id=record.id,
        meta={"status": record.status, "kind": record.kind},
        ip=actor.ip,
    )
    await session.flush()
    await session.refresh(record)
    out = _out(record)
    await session.commit()
    return out


@router.patch("/{item_id}", response_model=HeritageOut)
async def correct_item(
    item_id: uuid.UUID,
    payload: HeritageUpdate,
    actor: Actor = Depends(require(Permission.HERITAGE_MODERATE)),
    session: AsyncSession = Depends(get_session),
) -> HeritageOut:
    """Fix a typo, add the source somebody forgot.

    Editing does not re-open the review: a moderator correcting a published
    record's spelling should not unpublish it under the pilgrim reading it.
    """
    record = await _load(session, item_id)
    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(record, field, value)

    await audit_service.record(
        session,
        action=AuditAction.HERITAGE_CORRECTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="heritage_item",
        target_id=record.id,
        meta={"fields": sorted(changes.keys())},
        ip=actor.ip,
    )
    await session.flush()
    await session.refresh(record)
    out = _out(record)
    await session.commit()
    return out
