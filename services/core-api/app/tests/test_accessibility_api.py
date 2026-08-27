"""Accessibility, assistance and reserved darshan capacity (Track 1, item 4).

The rules pinned here are the ones that make this a feature rather than a form:

* a profile is declared once and read server-side — a client cannot claim priority;
* reserved seats are unreachable by an ordinary booking, and reachable by a
  pilgrim with a mobility need after the general pool is gone;
* a deaf pilgrim does not spend a seat held back for one who cannot walk;
* an unmet request must say why, and is recorded rather than deleted;
* the SLA clock starts when the pilgrim pressed, not when the phone reconnected;
* the volunteer board never carries the pilgrim's own notes about their body.

Needs Postgres and Redis (`docker compose up -d db redis`).
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, hash_phone, now_utc
from app.models import AssistanceRequest, Facility, Slot, Zone
from app.models.accessibility import RequestStatus
from app.services import accessibility_service

pytestmark = [pytest.mark.db, pytest.mark.redis]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def zone(session: AsyncSession) -> Zone:
    record = Zone(
        code="TC",
        name="Temple Core",
        name_mr="मंदिर गाभारा",
        geom="SRID=4326;POLYGON((75.329 17.678, 75.332 17.678, 75.332 17.681, 75.329 17.681, 75.329 17.678))",
        area_m2=1200.0,
        capacity_persons=2400,
        zone_type="temple_core",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def facility(session: AsyncSession, zone: Zone) -> Facility:
    record = Facility(
        zone_id=zone.id,
        type="toilet",
        name="Toilet Block A",
        name_mr="स्वच्छतागृह अ",
        location="SRID=4326;POINT(75.3305 17.6795)",
        status="operational",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def slot(session: AsyncSession) -> Slot:
    """A slot with two seats held back for pilgrims who cannot queue."""
    record = Slot(
        date=(now_utc() + timedelta(days=1)).date(),
        start_time=time(9, 0),
        end_time=time(9, 30),
        capacity=10,
        booked_count=0,
        walkin_reserve=2,
        assisted_reserve=2,
        assisted_used=0,
        status="open",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def pilgrim(make_user):
    return await make_user(phone="9876543210", role=Role.PILGRIM, password=None, name="यात्रेकरू")


@pytest.fixture
def pilgrim_token(pilgrim):
    token, _ = create_access_token(subject=str(pilgrim.id), role=pilgrim.role)
    return token


@pytest.fixture
async def volunteer_token(make_user):
    user = await make_user(phone="9000000002", role=Role.VOLUNTEER, name="स्वयंसेवक")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


async def declare(client: AsyncClient, api_prefix: str, token: str, needs: list[str], **kwargs) -> dict:
    body = {"needs": needs, **kwargs}
    response = await client.put(f"{api_prefix}/accessibility/me", json=body, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
async def test_a_pilgrim_declares_needs_once_and_reads_them_back(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    body = await declare(
        client,
        api_prefix,
        pilgrim_token,
        ["wheelchair", "step_free_route"],
        notes="डावा पाय, पायऱ्या चढता येत नाहीत",
        large_text=True,
    )
    assert body["needs"] == ["wheelchair", "step_free_route"]
    assert body["large_text"] is True
    assert body["priority_booking"] is True

    again = await client.get(f"{api_prefix}/accessibility/me", headers=bearer(pilgrim_token))
    assert again.status_code == 200
    assert again.json()["notes"] == "डावा पाय, पायऱ्या चढता येत नाहीत"


async def test_no_profile_reads_as_empty_rather_than_missing(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """The screen renders a form either way; a 404 would only be translated back into one."""
    response = await client.get(f"{api_prefix}/accessibility/me", headers=bearer(pilgrim_token))
    assert response.status_code == 200
    assert response.json()["needs"] == []
    assert response.json()["priority_booking"] is False


async def test_a_profile_is_replaced_whole_not_merged(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """Unticking "wheelchair" means they no longer need one."""
    await declare(client, api_prefix, pilgrim_token, ["wheelchair", "hearing"])
    body = await declare(client, api_prefix, pilgrim_token, ["hearing"])
    assert body["needs"] == ["hearing"]
    assert body["priority_booking"] is False


async def test_the_profile_contents_are_never_written_to_the_audit_log(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str
) -> None:
    """Health data in an append-only log outlives the pilgrim's ability to withdraw it."""
    from app.models import AuditLog
    from app.services.audit_service import AuditAction

    await declare(client, api_prefix, pilgrim_token, ["wheelchair"], notes="a private detail")

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.ACCESSIBILITY_PROFILE_SET)
    )
    assert entry is not None, "that a profile was set is worth knowing"
    assert entry.meta == {"need_count": 1}, "…what it said is not"


async def test_one_profile_per_pilgrim(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str
) -> None:
    from app.models import AccessibilityProfile

    await declare(client, api_prefix, pilgrim_token, ["wheelchair"])
    await declare(client, api_prefix, pilgrim_token, ["vision"])

    rows = await session.execute(select(AccessibilityProfile))
    assert len(list(rows.scalars())) == 1


# ---------------------------------------------------------------------------
# reserved darshan capacity — the part that changes who gets in
# ---------------------------------------------------------------------------
async def test_ordinary_bookings_cannot_touch_the_reserved_seats(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, slot: Slot
) -> None:
    """capacity 10 − walkin 2 − assisted 2 = 6 seats in the general pool."""
    response = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9876543210",
            "holder_name": "यात्रेकरू",
            "group_size": 6,
        },
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 201, response.text

    await session.refresh(slot)
    assert slot.booked_count == 6
    assert slot.assisted_used == 0


async def test_a_pilgrim_with_a_mobility_need_reaches_the_reserve_when_the_pool_is_gone(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    pilgrim_token: str,
    volunteer_token: str,
    slot: Slot,
) -> None:
    # Fill the general pool with ordinary bookings.
    slot.booked_count = 6
    await session.commit()

    ordinary = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9000000002",
            "holder_name": "स्वयंसेवक",
            "group_size": 1,
        },
        headers=bearer(volunteer_token),
    )
    assert ordinary.status_code == 409, "the general pool is exhausted"
    assert ordinary.json()["error"]["code"] == "SLOT_FULL"

    # The same slot, for somebody who cannot stand in the corridor.
    await declare(client, api_prefix, pilgrim_token, ["wheelchair"])
    assisted = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9876543210",
            "holder_name": "यात्रेकरू",
            "group_size": 1,
        },
        headers=bearer(pilgrim_token),
    )
    assert assisted.status_code == 201, assisted.text

    await session.refresh(slot)
    assert slot.assisted_used == 1
    assert slot.booked_count == 7


async def test_a_non_mobility_need_does_not_spend_the_mobility_reserve(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, slot: Slot
) -> None:
    """A deaf pilgrim needs different help, not a seat held for someone who cannot walk."""
    slot.booked_count = 6
    await session.commit()

    await declare(client, api_prefix, pilgrim_token, ["hearing"])
    response = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9876543210",
            "holder_name": "यात्रेकरू",
            "group_size": 1,
        },
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 409
    await session.refresh(slot)
    assert slot.assisted_used == 0


async def test_the_reserve_cannot_be_claimed_by_asking_for_it(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, slot: Slot
) -> None:
    """No request body field grants priority — the server reads the stored profile."""
    slot.booked_count = 6
    await session.commit()

    response = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9876543210",
            "holder_name": "यात्रेकरू",
            "group_size": 1,
            # Every hopeful spelling a client might try.
            "assisted": True,
            "priority": True,
            "needs": ["wheelchair"],
        },
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 409, "no declared profile, no reserved seat"
    await session.refresh(slot)
    assert slot.assisted_used == 0


async def test_the_reserve_is_finite(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, slot: Slot
) -> None:
    # Six general seats plus both reserved ones already gone. `assisted_used` is
    # a subset of `booked_count`, not a parallel counter — the reserved seats
    # are real seats in the room.
    slot.booked_count = 8
    slot.assisted_used = 2
    await session.commit()

    await declare(client, api_prefix, pilgrim_token, ["wheelchair"])
    response = await client.post(
        f"{api_prefix}/passes",
        json={
            "slot_id": str(slot.id),
            "phone": "9876543210",
            "holder_name": "यात्रेकरू",
            "group_size": 1,
        },
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# assistance requests
# ---------------------------------------------------------------------------
async def test_a_request_with_no_needs_fills_them_from_the_profile(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, zone: Zone
) -> None:
    """One button when you are stuck at a step, not a form you have already filled in."""
    await declare(client, api_prefix, pilgrim_token, ["wheelchair", "step_free_route"])

    response = await client.post(
        f"{api_prefix}/assistance",
        json={"zone_id": str(zone.id)},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["needs"] == ["wheelchair", "step_free_route"]
    assert body["reference"].startswith("AS-")
    assert body["status"] == "open"
    assert body["zone_code"] == "TC"


async def test_the_sla_clock_starts_when_the_pilgrim_pressed_not_when_it_arrived(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """A phone that syncs ten minutes late must not hand the desk a fresh clock."""
    pressed = now_utc() - timedelta(minutes=12)
    response = await client.post(
        f"{api_prefix}/assistance",
        json={"needs": ["wheelchair"], "client_reported_at": pressed.isoformat()},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["waiting_seconds"] > 600
    # 15-minute SLA measured from the press: three minutes left, not fifteen.
    assert body["sla_breached"] is False


async def test_the_board_shows_the_most_overdue_first_and_never_the_private_notes(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, volunteer_token: str
) -> None:
    await declare(client, api_prefix, pilgrim_token, ["wheelchair"], notes="a private detail")

    old = now_utc() - timedelta(minutes=40)
    await client.post(
        f"{api_prefix}/assistance",
        json={"needs": ["wheelchair"], "client_reported_at": old.isoformat()},
        headers=bearer(pilgrim_token),
    )
    await client.post(
        f"{api_prefix}/assistance", json={"needs": ["vision"]}, headers=bearer(pilgrim_token)
    )

    response = await client.get(f"{api_prefix}/assistance", headers=bearer(volunteer_token))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2
    # Most overdue first, and it has breached.
    assert items[0]["needs"] == ["wheelchair"]
    assert items[0]["sla_breached"] is True
    # The volunteer learns what to bring, not what the pilgrim wrote about themselves.
    for entry in items:
        assert "a private detail" not in str(entry)


async def test_a_pilgrim_cannot_read_the_board(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    response = await client.get(f"{api_prefix}/assistance", headers=bearer(pilgrim_token))
    assert response.status_code == 403


async def test_a_volunteer_claims_a_request_and_the_clock_stops(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, volunteer_token: str
) -> None:
    raised = await client.post(
        f"{api_prefix}/assistance", json={"needs": ["wheelchair"]}, headers=bearer(pilgrim_token)
    )
    request_id = raised.json()["id"]

    response = await client.patch(
        f"{api_prefix}/assistance/{request_id}", json={"claim": True}, headers=bearer(volunteer_token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "assigned"
    assert response.json()["assigned_at"] is not None


async def test_closing_as_unmet_requires_saying_why(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, volunteer_token: str
) -> None:
    """The number that matters after a Wari is how many asks went unanswered."""
    raised = await client.post(
        f"{api_prefix}/assistance", json={"needs": ["wheelchair"]}, headers=bearer(pilgrim_token)
    )
    request_id = raised.json()["id"]

    bare = await client.patch(
        f"{api_prefix}/assistance/{request_id}",
        json={"status": "unmet"},
        headers=bearer(volunteer_token),
    )
    assert bare.status_code == 400
    assert bare.json()["error"]["code"] == "OUTCOME_NOTE_REQUIRED"

    explained = await client.patch(
        f"{api_prefix}/assistance/{request_id}",
        json={"status": "unmet", "outcome_note": "कोणतीही चाकाची खुर्ची उपलब्ध नव्हती"},
        headers=bearer(volunteer_token),
    )
    assert explained.status_code == 200, explained.text
    assert explained.json()["status"] == "unmet"


async def test_a_pilgrim_may_cancel_their_own_request_and_nothing_else(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    raised = await client.post(
        f"{api_prefix}/assistance", json={"needs": ["wheelchair"]}, headers=bearer(pilgrim_token)
    )
    request_id = raised.json()["id"]

    # "Never mind, my son found me."
    cancelled = await client.patch(
        f"{api_prefix}/assistance/{request_id}",
        json={"status": "cancelled"},
        headers=bearer(pilgrim_token),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


async def test_a_pilgrim_cannot_mark_their_own_request_met(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """`met` is an assertion about the physical world, made by whoever was there."""
    raised = await client.post(
        f"{api_prefix}/assistance", json={"needs": ["wheelchair"]}, headers=bearer(pilgrim_token)
    )
    response = await client.patch(
        f"{api_prefix}/assistance/{raised.json()['id']}",
        json={"status": "met"},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# facility survey
# ---------------------------------------------------------------------------
async def test_a_survey_merges_rather_than_erasing_last_weeks_answer(
    client: AsyncClient, api_prefix: str, session: AsyncSession, volunteer_token: str, facility: Facility
) -> None:
    first = await client.patch(
        f"{api_prefix}/accessibility/facilities/{facility.id}",
        json={"step_free": True},
        headers=bearer(volunteer_token),
    )
    assert first.status_code == 200, first.text

    second = await client.patch(
        f"{api_prefix}/accessibility/facilities/{facility.id}",
        json={"accessible_toilet": True},
        headers=bearer(volunteer_token),
    )
    assert second.json()["accessibility"] == {"step_free": True, "accessible_toilet": True}


def test_an_unsurveyed_facility_is_not_treated_as_step_free() -> None:
    """Sending a wheelchair to a step nobody checked is worse than saying we do not know."""
    assert accessibility_service.clean_facility_flags({}) == {}
    # A typo must not become a stored key that reads as an answer.
    assert accessibility_service.clean_facility_flags({"step-free": True}) == {}
    assert accessibility_service.clean_facility_flags({"step_free": True}) == {"step_free": True}


def test_needs_are_stored_in_a_stable_order() -> None:
    """Two pilgrims who tick the same boxes must produce comparable rows."""
    one = accessibility_service.clean_needs(["hearing", "wheelchair"])
    two = accessibility_service.clean_needs(["wheelchair", "hearing"])
    assert one == two
    # Unknown values are dropped rather than stored.
    assert accessibility_service.clean_needs(["wheelchair", "nonsense"]) == ["wheelchair"]
