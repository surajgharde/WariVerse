"""Slot availability and darshan pass endpoints (Section 9).

    GET  /slots                 POST /passes
    GET  /passes/{id}           GET  /passes/{id}/qr
    POST /passes/{id}/cancel

Phase 7 adds what the pilgrim app needs to survive a reinstall and a dead
network:

    GET  /me/passes             GET  /me/notifications
    POST /passes/{id}/card-link GET  /passes/{id}/card
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from html import escape

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, get_optional_actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import hash_phone, now_utc, sign_card_link, verify_card_link
from app.models import Gate, Pass, PassStatus
from app.schemas.common import Ack, ErrorResponse
from app.schemas.passes import (
    CardLink,
    MyPasses,
    NotificationList,
    NotificationOut,
    PassCreate,
    PassIssued,
    PassOut,
    QrOut,
    SlotGrid,
    SlotOut,
)
from app.services import (
    accessibility_service,
    audit_service,
    config_service,
    pass_service,
    qr_image,
    qr_service,
    slot_service,
)
from app.services.audit_service import AuditAction

router = APIRouter(tags=["passes"], responses={404: {"model": ErrorResponse}})


@router.get("/slots", response_model=SlotGrid)
async def get_slots(
    day: date = Query(default=None, alias="date", description="Defaults to today"),
    session: AsyncSession = Depends(get_session),
) -> SlotGrid:
    """The day's availability grid.

    Public: a pilgrim decides whether it is worth travelling before they sign
    in (Section 2 — no login required to browse).
    """
    target = day or now_utc().date()
    slots = await pass_service.list_slots(session, target)

    gate_codes: dict[uuid.UUID, str] = {}
    for slot in slots:
        if slot.gate_id and slot.gate_id not in gate_codes:
            gate = await session.get(Gate, slot.gate_id)
            if gate:
                gate_codes[slot.gate_id] = gate.code

    items: list[SlotOut] = []
    total = 0
    for slot in slots:
        available = slot_service.available_seats(
            slot_service.SlotState(
                capacity=slot.capacity,
                booked_count=slot.booked_count,
                walkin_reserve=slot.walkin_reserve,
                status=slot.status,
                walkin_used=slot.walkin_used,
            )
        )
        total += available
        start, _ = pass_service.slot_bounds(slot)
        items.append(
            SlotOut(
                id=slot.id,
                date=slot.date,
                start_time=slot.start_time,
                end_time=slot.end_time,
                capacity=slot.capacity,
                booked_count=slot.booked_count,
                walkin_reserve=slot.walkin_reserve,
                available=available,
                status=slot.status,
                gate_code=gate_codes.get(slot.gate_id) if slot.gate_id else None,
                is_bookable=available > 0 and start > now_utc(),
            )
        )

    await session.commit()
    return SlotGrid(
        date=target,
        slots=items,
        total_available=total,
        walkin_reserve_pct=await config_service.get_float(session, "walkin_reserve_pct"),
        generated_at=now_utc(),
    )


async def _to_out(session: AsyncSession, record: Pass) -> PassOut:
    view = await pass_service.describe_pass(session, record)
    start, end = pass_service.slot_bounds(view.slot)
    return PassOut(
        id=record.id,
        reference=record.reference,
        status=record.status,
        group_size=record.group_size,
        holder_name=record.holder_name,
        slot_date=view.slot.date,
        slot_start=start,
        slot_end=end,
        gate_code=view.gate_code,
        issued_at=record.issued_at,
        scanned_at=record.scanned_at,
        estimated_entry_at=view.eta,
        queue_ahead=view.queue_ahead,
        reslot_count=record.reslot_count,
        was_reslotted=view.is_reslotted,
        allow_early_reslot=record.allow_early_reslot,
        as_of=now_utc(),
    )


@router.post("/passes", response_model=PassIssued, status_code=201)
async def book_pass(
    payload: PassCreate,
    actor: Actor = Depends(require(Permission.PASS_BOOK)),
    session: AsyncSession = Depends(get_session),
) -> PassIssued:
    """Book a Smart Darshan Pass for up to six people.

    The caller must be signed in with a verified phone; the pass is bound to
    that phone's hash, never to the number itself.
    """
    phone_hash = hash_phone(payload.phone)
    if phone_hash != actor.user.phone_hash:
        raise AppError(
            "FORBIDDEN",
            message="You can only book with the phone number you signed in with.",
            message_mr="तुम्ही ज्या नंबरने साइन इन केले त्याच नंबरने नोंदणी करू शकता.",
        )

    booked_today = await pass_service.count_passes_today(session, phone_hash)
    if booked_today >= settings.rate_limit_pass_booking_per_day:
        raise AppError(
            "BOOKING_LIMIT_REACHED",
            details={"limit": settings.rate_limit_pass_booking_per_day, "booked": booked_today},
        )

    if payload.members and len(payload.members) > payload.group_size:
        raise AppError(
            "BAD_REQUEST",
            message="More members listed than the group size.",
            message_mr="गटाच्या संख्येपेक्षा जास्त नावे दिली आहेत.",
            details={"group_size": payload.group_size, "members": len(payload.members)},
        )

    # Read from the caller's stored profile, never from the request body
    # (Track 1, item 4). A body field would be set by every client within a
    # week and the reserved seats would be gone by lunchtime on day one.
    assisted = await accessibility_service.assisted_booking_allowed(session, actor.id)

    record = await pass_service.book_pass(
        session,
        slot_id=payload.slot_id,
        phone_hash=phone_hash,
        holder_name=payload.holder_name,
        group_size=payload.group_size,
        language=payload.language,
        members=[(m.name, m.age_band) for m in payload.members],
        allow_early_reslot=payload.allow_early_reslot,
        assisted=assisted,
    )

    await audit_service.record(
        session,
        action=AuditAction.PASS_ISSUED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="pass",
        target_id=record.id,
        meta={"reference": record.reference, "slot_id": str(payload.slot_id), "group_size": record.group_size},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    qr_payload, valid_for = await pass_service.qr_material(session, record)
    base = await _to_out(session, record)
    await session.commit()

    return PassIssued(
        **base.model_dump(),
        # Returned once, at issue.  The device stores it and computes the
        # rolling code locally, which is what makes the QR work offline.
        qr_secret=record.qr_secret,
        qr_payload=qr_payload,
        qr_valid_for_seconds=valid_for,
    )


def _assert_owner(actor: Actor, record: Pass) -> None:
    if record.holder_phone_hash != actor.user.phone_hash and not actor.can(Permission.PASS_ADMIN):
        raise AppError("PASS_NOT_FOUND", details={"pass_id": str(record.id)})


@router.get("/passes/{pass_id}", response_model=PassOut)
async def get_pass(
    pass_id: uuid.UUID,
    actor: Actor | None = Depends(get_optional_actor),
    session: AsyncSession = Depends(get_session),
) -> PassOut:
    """Pass status with a live wait estimate."""
    record = await pass_service.load_pass(session, pass_id)
    if actor is None:
        raise AppError("UNAUTHENTICATED")
    _assert_owner(actor, record)
    out = await _to_out(session, record)
    await session.commit()
    return out


@router.get("/passes/{pass_id}/qr", response_model=QrOut)
async def get_pass_qr(
    pass_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.PASS_VIEW_OWN)),
    session: AsyncSession = Depends(get_session),
) -> QrOut:
    """Current QR payload.

    Only needed when the device cannot compute the rolling code itself — the
    offline path is to keep `qr_secret` from booking and generate locally.
    """
    record = await pass_service.load_pass(session, pass_id)
    _assert_owner(actor, record)
    payload, valid_for = await pass_service.qr_material(session, record)
    await session.commit()
    return QrOut(
        qr_payload=payload,
        valid_for_seconds=valid_for,
        rotates_every_seconds=qr_service.ROLLING_STEP_SECONDS,
        as_of=now_utc(),
    )


@router.post("/passes/{pass_id}/cancel", response_model=Ack)
async def cancel_pass(
    pass_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.PASS_CANCEL_OWN)),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Cancel a pass and return its seats to the pool immediately."""
    record = await pass_service.load_pass(session, pass_id)
    _assert_owner(actor, record)
    await pass_service.cancel_pass(session, record)

    await audit_service.record(
        session,
        action=AuditAction.PASS_CANCELLED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="pass",
        target_id=record.id,
        meta={"reference": record.reference},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return Ack(message="Pass cancelled.", message_mr="पास रद्द केला.")


# ---------------------------------------------------------------------------
# the pilgrim's own view (Phase 7)
# ---------------------------------------------------------------------------
@router.get("/me/passes", response_model=MyPasses)
async def my_passes(
    actor: Actor = Depends(require(Permission.PASS_VIEW_OWN)),
    session: AsyncSession = Depends(get_session),
) -> MyPasses:
    """Every pass on the caller's phone number.

    The app cannot rely on remembering a pass id.  A reinstall, a cleared cache
    or a second handset all leave a signed-in pilgrim with a pass they cannot
    open, and the person that happens to is standing in a queue.  Their phone
    hash is the durable handle; this route is how the app recovers from it.
    """
    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    now = now_utc()

    upcoming: list[PassOut] = []
    past: list[PassOut] = []
    for record in await pass_service.list_passes_for(session, actor.user.phone_hash):
        out = await _to_out(session, record)
        still_live = record.status == PassStatus.ACTIVE and out.slot_end + timedelta(minutes=grace) > now
        (upcoming if still_live else past).append(out)

    # Soonest first among the live ones — the one they are about to use is the
    # one the app opens on.
    upcoming.sort(key=lambda p: p.slot_start)
    await session.commit()
    return MyPasses(upcoming=upcoming, past=past, generated_at=now)


@router.get("/me/notifications", response_model=NotificationList)
async def my_notifications(
    actor: Actor = Depends(require(Permission.PASS_VIEW_OWN)),
    session: AsyncSession = Depends(get_session),
) -> NotificationList:
    """Messages queued about the caller's passes — reslots, above all.

    Section 4/M1 requires a reslotted pilgrim to be told *why* their time moved.
    Until the notifier service exists there is no push and no SMS, so the app
    reads the outbox and shows the message with its real `queued` status rather
    than implying anything was delivered.
    """
    rows = await pass_service.notifications_for(session, actor.user.phone_hash)
    await session.commit()
    return NotificationList(
        items=[
            NotificationOut(
                id=note.id,
                pass_id=note.pass_id,
                pass_reference=record.reference,
                type=note.type,
                message=note.payload_en,
                message_mr=note.payload_mr,
                status=note.status,
                created_at=note.created_at,
            )
            for note, record in rows
        ],
        generated_at=now_utc(),
    )


# ---------------------------------------------------------------------------
# no-JavaScript pass card (Section 4/M7)
# ---------------------------------------------------------------------------
async def _card_expiry(session: AsyncSession, record: Pass) -> datetime:
    """A card link dies exactly when the pass it opens does."""
    view = await pass_service.describe_pass(session, record)
    _, end = pass_service.slot_bounds(view.slot)
    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    return end + timedelta(minutes=grace)


@router.post("/passes/{pass_id}/card-link", response_model=CardLink)
async def create_card_link(
    pass_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(require(Permission.PASS_VIEW_OWN)),
    session: AsyncSession = Depends(get_session),
) -> CardLink:
    """Mint the signed URL for this pass's plain-HTML card.

    For the phone that cannot run the app: a 2016 handset whose browser chokes
    on the bundle, a device with JavaScript disabled, or an install that failed
    on 2G.  The pilgrim opens the link once and can reopen it from history for
    as long as the pass lives.
    """
    record = await pass_service.load_pass(session, pass_id)
    _assert_owner(actor, record)
    if record.status == PassStatus.CANCELLED:
        raise AppError("PASS_CANCELLED")

    expires_at = await _card_expiry(session, record)
    token = sign_card_link(str(record.id), expires_at)
    path = f"{settings.api_v1_prefix}/passes/{record.id}/card?k={token}"
    await session.commit()

    return CardLink(
        url=str(request.base_url).rstrip("/") + path,
        expires_at=expires_at,
        note="Anyone with this link can show your pass. Treat it like the pass itself.",
        note_mr="ही लिंक असलेली कोणतीही व्यक्ती तुमचा पास दाखवू शकते. ती पासाप्रमाणेच जपून ठेवा.",
    )


#: Inline everything.  A card page that fetches a stylesheet is a card page that
#: renders unstyled on the network this exists for.
_CARD_PAGE = """<!doctype html>
<html lang="mr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="{refresh}">
<title>{reference} — दर्शन पास</title>
<style>
  body {{ margin:0; padding:16px; background:#FAF7F2; color:#16181D;
         font-family:system-ui,'Noto Sans Devanagari',sans-serif; font-size:18px; line-height:1.5; }}
  main {{ max-width:420px; margin:0 auto; }}
  .qr {{ background:#fff; border:2px solid #1B2A5E; border-radius:12px; padding:12px; }}
  .qr svg {{ width:100%; height:auto; display:block; }}
  .ref {{ font-size:32px; font-weight:700; letter-spacing:2px; margin:16px 0 4px; }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:8px 16px; margin:16px 0; }}
  dt {{ color:#4a4f5c; }}
  dd {{ margin:0; font-weight:600; }}
  .note {{ background:#fff; border-left:4px solid #E8622B; padding:12px; font-size:16px; }}
  .stale {{ background:#C42B1C; color:#fff; padding:12px; border-radius:8px; font-weight:600; }}
</style>
</head>
<body>
<main>
  <div class="qr">{qr}</div>
  <p class="ref">{reference}</p>
  <dl>
    <dt>नाव</dt><dd>{holder}</dd>
    <dt>वेळ</dt><dd>{window}</dd>
    <dt>व्यक्ती</dt><dd>{group_size}</dd>
    <dt>द्वार</dt><dd>{gate}</dd>
  </dl>
  <p class="note">हा कोड दर {step} सेकंदांनी बदलतो. हे पान आपोआप पुन्हा उघडते —
     स्क्रीनशॉट चालणार नाही.<br>
     <span lang="en">This code changes every {step} seconds. The page reloads itself;
     a screenshot will not work.</span></p>
</main>
</body>
</html>
"""

_CARD_DEAD_PAGE = """<!doctype html>
<html lang="mr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>दर्शन पास</title>
<style>body{{margin:0;padding:24px;background:#FAF7F2;color:#16181D;
font-family:system-ui,'Noto Sans Devanagari',sans-serif;font-size:18px;line-height:1.5}}
.stale{{background:#C42B1C;color:#fff;padding:16px;border-radius:8px;font-weight:600}}</style>
</head><body><main>
<p class="stale">{message_mr}</p>
<p lang="en">{message}</p>
</main></body></html>
"""


@router.get("/passes/{pass_id}/card", response_class=HTMLResponse, include_in_schema=False)
async def pass_card(
    pass_id: uuid.UUID,
    k: str = Query(description="Signed card link from POST /passes/{id}/card-link"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """The pass, as a page that needs nothing but HTML.

    Section 4/M7's performance budget ends with "fully usable without JS for the
    pass QR". This is that page: server-rendered, no script tag, no external
    request, one inline SVG.

    The rolling code inside the QR is only good for sixty seconds, so the page
    carries a `<meta refresh>` a little under that. A meta refresh is a blunt
    instrument and is the right one here — it is the only way to keep a live
    credential current in a browser that is not running our code.

    Not audited: this is the holder reading their own pass, the same act as
    opening the app, and writing an append-only row every 55 seconds for every
    pilgrim on a no-JS phone would drown the log that Phase 6 depends on.
    """
    verify_card_link(str(pass_id), k)
    record = await pass_service.load_pass(session, pass_id)

    if record.status != PassStatus.ACTIVE:
        dead = {
            PassStatus.SCANNED: ("This pass has been used.", "हा पास वापरला गेला आहे."),
            PassStatus.CANCELLED: ("This pass was cancelled.", "हा पास रद्द केला आहे."),
            PassStatus.EXPIRED: ("This pass has expired.", "या पासची मुदत संपली आहे."),
        }[PassStatus(record.status)]
        await session.commit()
        return HTMLResponse(
            _CARD_DEAD_PAGE.format(message=dead[0], message_mr=dead[1]),
            status_code=410,
            headers={"cache-control": "no-store"},
        )

    payload, _ = await pass_service.qr_material(session, record)
    view = await pass_service.describe_pass(session, record)
    start, end = pass_service.slot_bounds(view.slot)
    await session.commit()

    body = _CARD_PAGE.format(
        qr=qr_image.svg(payload),
        reference=escape(record.reference),
        holder=escape(record.holder_name),
        window=f"{start:%d/%m} {view.slot.start_time:%H:%M} - {view.slot.end_time:%H:%M}",
        group_size=record.group_size,
        gate=escape(view.gate_code or "—"),
        step=qr_service.ROLLING_STEP_SECONDS,
        # Reload just inside the rotation window so the code on screen is never
        # the previous one. Five seconds of margin covers a slow 2G round trip.
        refresh=qr_service.ROLLING_STEP_SECONDS - 5,
    )
    # A live credential must not sit in a proxy or a browser's back-forward cache.
    return HTMLResponse(body, headers={"cache-control": "no-store, must-revalidate"})
