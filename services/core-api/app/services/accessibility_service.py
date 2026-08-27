"""Accessibility profiles, assistance requests, and the reserved capacity.

The rules that are worth stating, because they are the ones a later refactor
will quietly drop:

**A profile is declared once and read by the server, never sent by the client
as a claim.** `assisted_booking_allowed` reads the stored profile. The booking
route never accepts "I need priority" from a request body — a flag a client can
set is a flag every client eventually sets, and the reserve drains in an hour.

**An unmet request is closed as `unmet`, not deleted.** The number that matters
after a Wari is not how many wheelchairs were delivered; it is how many were
asked for and never came. A schema with no way to record that produces a
flawless report and a false one.

**Assistance requests are not incidents.** They have their own board and their
own single SLA. Grading a wheelchair against a cardiac arrest on one list either
starves the emergency or buries the desk; and the right responder is a volunteer
with a chair, not an ambulance.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import (
    AccessibilityProfile,
    AssistanceRequest,
    Facility,
)
from app.models.accessibility import (
    ASSISTANCE_SLA_MINUTES,
    MOBILITY_NEEDS,
    OPEN_REQUEST_STATUSES,
    AssistanceNeed,
    RequestStatus,
)

logger = get_logger(__name__)

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Keys a facility survey may set. Anything else is dropped rather than stored:
#: an unbounded bag on a public read is a place for junk to accumulate and for a
#: typo (`step-free` vs `step_free`) to silently mean "unsurveyed" forever.
FACILITY_ACCESS_KEYS: frozenset[str] = frozenset(
    {"step_free", "ramp", "accessible_toilet", "seating", "staffed", "handrail"}
)


def reference() -> str:
    return "AS-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def sla_due(from_time=None):
    return (from_time or now_utc()) + timedelta(minutes=ASSISTANCE_SLA_MINUTES)


def clean_needs(needs: list[str] | None) -> list[str]:
    """Keep the known values, in a stable order, without duplicates.

    Order is the enum's, not the caller's, so two pilgrims who tick the same
    boxes in different orders produce the same row — which is what makes the
    stored list comparable in a report.
    """
    given = set(needs or [])
    return [need.value for need in AssistanceNeed if need.value in given]


def clean_facility_flags(flags: dict[str, object] | None) -> dict[str, bool]:
    return {
        key: bool(value)
        for key, value in (flags or {}).items()
        if key in FACILITY_ACCESS_KEYS
    }


async def get_profile(session: AsyncSession, user_id: uuid.UUID) -> AccessibilityProfile | None:
    return await session.scalar(
        select(AccessibilityProfile).where(AccessibilityProfile.user_id == user_id)
    )


async def upsert_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    needs: list[str],
    notes: str | None,
    large_text: bool,
    high_contrast: bool,
    companion_phone_hash: str | None = None,
) -> AccessibilityProfile:
    """One row per pilgrim, replaced whole.

    Replaced rather than merged because the screen that writes it shows every
    need at once: a pilgrim who unticks "wheelchair" means they no longer need
    one, and a merge would keep it forever.
    """
    profile = await get_profile(session, user_id)
    cleaned = clean_needs(needs)

    if profile is None:
        profile = AccessibilityProfile(
            user_id=user_id,
            needs=cleaned,
            notes=notes,
            large_text=large_text,
            high_contrast=high_contrast,
            companion_phone_hash=companion_phone_hash,
        )
        session.add(profile)
        await session.flush()
        return profile

    profile.needs = cleaned
    profile.notes = notes
    profile.large_text = large_text
    profile.high_contrast = high_contrast
    if companion_phone_hash is not None:
        profile.companion_phone_hash = companion_phone_hash

    # `updated_at` is `onupdate=func.now()`, so the flush expires it and the
    # caller reading it back would trigger a lazy load outside the greenlet —
    # `MissingGreenlet`, at the point the response is being built. Refreshing
    # here is what lets the route serialise the row it just wrote.
    await session.flush()
    await session.refresh(profile)
    return profile


async def assisted_booking_allowed(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """May this pilgrim draw on the reserved darshan seats?

    Only a *mobility* need qualifies. A pilgrim who is deaf needs a different
    kind of help and does not need a seat held back from somebody who cannot
    walk — spending the reserve on needs it was not held for is how a reserve
    stops being one.
    """
    profile = await get_profile(session, user_id)
    return bool(profile and profile.has_mobility_need())


def needs_summary(needs: list[str] | None) -> list[str]:
    """What a dispatched volunteer is told.

    The needs, and never the notes. The notes are the pilgrim's own words about
    their body; a volunteer bringing a wheelchair needs "wheelchair", and the
    rest is theirs.
    """
    return [need for need in (needs or []) if need in {n.value for n in AssistanceNeed}]


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
async def raise_request(
    session: AsyncSession,
    *,
    needs: list[str],
    requested_by: uuid.UUID | None,
    on_behalf_of: str | None = None,
    zone_id: uuid.UUID | None = None,
    gate_id: uuid.UUID | None = None,
    facility_id: uuid.UUID | None = None,
    note: str | None = None,
    language: str = "mr",
    requested_at=None,
) -> AssistanceRequest:
    """Open a request, filling the needs from the caller's profile if they gave none.

    Falling back to the profile matters more than it looks: a pilgrim who has
    already told the system they use a wheelchair should be able to press one
    button, and being made to re-declare it at the moment they are stuck at a
    step is the exact indignity the profile exists to prevent.
    """
    cleaned = clean_needs(needs)
    if not cleaned and requested_by:
        profile = await get_profile(session, requested_by)
        cleaned = clean_needs(list(profile.needs) if profile else [])

    at = requested_at or now_utc()
    record = AssistanceRequest(
        reference=reference(),
        requested_by=requested_by,
        on_behalf_of=on_behalf_of,
        needs=cleaned,
        note=note,
        zone_id=zone_id,
        gate_id=gate_id,
        facility_id=facility_id,
        status=RequestStatus.OPEN,
        requested_at=at,
        # From when the pilgrim asked, not from when the request reached us. A
        # phone that syncs a queued request ten minutes late must not hand the
        # desk a fresh clock — the same rule the SOS path follows.
        sla_due_at=sla_due(at),
        language=language,
    )
    session.add(record)
    await session.flush()
    return record


async def load_request(session: AsyncSession, request_id: uuid.UUID) -> AssistanceRequest:
    record = await session.get(AssistanceRequest, request_id)
    if record is None:
        raise AppError("ASSISTANCE_NOT_FOUND")
    return record


def assign(record: AssistanceRequest, volunteer_id: uuid.UUID) -> None:
    if record.status not in OPEN_REQUEST_STATUSES:
        raise AppError("ASSISTANCE_CLOSED", details={"status": record.status})
    record.assigned_to = volunteer_id
    record.assigned_at = now_utc()
    record.status = RequestStatus.ASSIGNED


def close(record: AssistanceRequest, *, status: RequestStatus, outcome_note: str | None) -> None:
    """Close a request, and refuse to close it as `unmet` without saying why.

    Same rule as closing an incident. "Nobody came" is the outcome most worth
    counting after a Wari, and an unexplained one is a row nobody can learn
    from.
    """
    if status not in (RequestStatus.MET, RequestStatus.CANCELLED, RequestStatus.UNMET):
        raise AppError("INVALID_TRANSITION", details={"status": status})
    if status == RequestStatus.UNMET and not (outcome_note or "").strip():
        raise AppError("OUTCOME_NOTE_REQUIRED")

    record.status = status
    record.outcome_note = outcome_note
    record.resolved_at = now_utc()


def is_breached(record: AssistanceRequest) -> bool:
    """Late means nobody was assigned in time — not that help has not finished.

    A volunteer who reached the pilgrim in four minutes and is still pushing the
    chair has not breached anything.
    """
    if record.assigned_at is not None:
        return record.assigned_at > record.sla_due_at
    return record.status in OPEN_REQUEST_STATUSES and now_utc() > record.sla_due_at


async def accessible_facilities(
    session: AsyncSession, *, require_step_free: bool = False
) -> list[Facility]:
    """Facilities a pilgrim with a mobility need can actually use.

    Unsurveyed facilities (`accessibility == {}`) are excluded from a step-free
    filter rather than assumed passable. The pilgrim UI shows them separately as
    "not known" — sending somebody in a wheelchair to a step we never checked is
    worse than saying we do not know.
    """
    rows = await session.execute(select(Facility).where(Facility.status != "out_of_service"))
    facilities = list(rows.scalars())
    if not require_step_free:
        return facilities
    return [f for f in facilities if (f.accessibility or {}).get("step_free") is True]
