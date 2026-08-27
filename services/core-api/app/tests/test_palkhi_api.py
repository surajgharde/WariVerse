"""Palkhi tracking end to end (Section 4/M8, Phase 9).

These need Postgres with PostGIS: the route projection, the arrival detection
and the readiness board are all spatial queries, and testing them against a
stub would test the stub.

What is checked here that `test_palkhi_rules.py` cannot check, because it needs
real geometry and real rows:

* a ping lands, projects onto the route line, and comes back with a
  battery-aware interval for the phone;
* the designated-device rule actually refuses a second phone;
* walking into a halt town records the arrival against the schedule;
* the readiness board sums head counts from the schedule and flags a town that
  claims to be ready without the provisioning to support it;
* the leader's phone number is never in a list response, and reading it writes
  an audit row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, hash_phone
from app.models import AuditLog, Dindi, DindiScheduleStop, HaltTown, Route, User

pytestmark = [pytest.mark.db, pytest.mark.redis]

# A short synthetic route: two degrees of longitude of straight road, so
# fractions along it are easy to reason about in a test.
ROUTE_START_LON, ROUTE_LAT = 74.0, 18.0
ROUTE_END_LON = 75.0


def token_for(user: User) -> str:
    token, _expires = create_access_token(
        subject=str(user.id), role=user.role, mfa_verified=True
    )
    return token


@pytest.fixture
async def route(session: AsyncSession) -> Route:
    row = Route(
        name="Alandi Palkhi Route",
        name_mr="आळंदी पालखी मार्ग",
        origin="Alandi",
        path=f"SRID=4326;LINESTRING({ROUTE_START_LON} {ROUTE_LAT}, {ROUTE_END_LON} {ROUTE_LAT})",
        total_km=100.0,
        year=2026,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
async def towns(session: AsyncSession, route: Route) -> list[HaltTown]:
    made: list[HaltTown] = []
    # At 25% and 60% along the line.
    for index, (name, name_mr, fraction) in enumerate(
        [("Saswad", "सासवड", 0.25), ("Lonand", "लोणंद", 0.60)], start=1
    ):
        lon = ROUTE_START_LON + fraction * (ROUTE_END_LON - ROUTE_START_LON)
        town = HaltTown(
            name=name,
            name_mr=name_mr,
            route_id=route.id,
            sequence=index,
            centroid=f"SRID=4326;POINT({lon} {ROUTE_LAT})",
            water_points=0,
            sanitation_units=0,
            medical_camps=0,
            readiness_status="unknown",
        )
        session.add(town)
        made.append(town)
    await session.commit()
    return made


@pytest.fixture
async def admin(make_user) -> User:
    return await make_user(phone="9100000001", name="Admin", role=Role.ADMINISTRATOR)


@pytest.fixture
async def volunteer(make_user) -> User:
    return await make_user(phone="9100000002", name="Volunteer", role=Role.VOLUNTEER)


@pytest.fixture
async def pilgrim(make_user) -> User:
    return await make_user(phone="9100000003", name="Pilgrim", role=Role.PILGRIM, password=None)


async def register(client: AsyncClient, api_prefix, auth_headers, admin, route, **overrides) -> dict:
    body = {
        "code": "DND-014",
        "name": "Sant Tukaram Dindi 14",
        "name_mr": "संत तुकाराम दिंडी १४",
        "leader_name": "Ramesh Pawar",
        "leader_phone": "9822012345",
        "expected_count": 400,
        "route_id": str(route.id),
        **overrides,
    }
    response = await client.post(
        f"{api_prefix}/dindis", json=body, headers=auth_headers(token_for(admin))
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
async def test_registering_a_dindi_stores_no_phone_number_in_the_clear(
    client, api_prefix, auth_headers, admin, route, session
):
    """Section 8's PII rule. The entity holds an HMAC; the number lives
    encrypted in `contact_secrets` with a season TTL."""
    body = await register(client, api_prefix, auth_headers, admin, route)

    dindi = await session.get(Dindi, uuid.UUID(body["id"]))
    assert dindi is not None
    assert dindi.leader_phone_hash == hash_phone("9822012345")
    assert "9822012345" not in str(body)


async def test_a_duplicate_code_is_refused(client, api_prefix, auth_headers, admin, route):
    await register(client, api_prefix, auth_headers, admin, route)
    response = await client.post(
        f"{api_prefix}/dindis",
        json={
            "code": "DND-014",
            "name": "Another Dindi",
            "name_mr": "दुसरी दिंडी",
            "leader_name": "Sunil Jadhav",
            "leader_phone": "9822012399",
            "expected_count": 100,
        },
        headers=auth_headers(token_for(admin)),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DINDI_CODE_TAKEN"


async def test_a_volunteer_cannot_register_a_dindi(
    client, api_prefix, auth_headers, volunteer, route
):
    """Registration writes a leader's phone number. That is an administrator's
    decision; confirming a town's water tankers is not."""
    response = await client.post(
        f"{api_prefix}/dindis",
        json={
            "code": "DND-099",
            "name": "Volunteer Attempt",
            "name_mr": "स्वयंसेवक प्रयत्न",
            "leader_name": "Sunil Jadhav",
            "leader_phone": "9822012300",
            "expected_count": 100,
        },
        headers=auth_headers(token_for(volunteer)),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------
async def set_schedule(client, api_prefix, auth_headers, admin, dindi_id, towns, *, base=None):
    start = base or datetime.now(UTC) + timedelta(hours=4)
    response = await client.put(
        f"{api_prefix}/dindis/{dindi_id}/schedule",
        json={
            "stops": [
                {"halt_town_id": str(towns[0].id), "planned_arrival": start.isoformat()},
                {
                    "halt_town_id": str(towns[1].id),
                    "planned_arrival": (start + timedelta(days=1)).isoformat(),
                },
            ]
        },
        headers=auth_headers(token_for(admin)),
    )
    return response


async def test_a_schedule_is_stored_in_walking_order(
    client, api_prefix, auth_headers, admin, route, towns
):
    body = await register(client, api_prefix, auth_headers, admin, route)
    response = await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert [s["sequence"] for s in schedule] == [1, 2]
    assert [s["halt_town"] for s in schedule] == ["Saswad", "Lonand"]
    # No arrivals yet, so no deviation to report.
    assert all(s["arrival_deviation_minutes"] is None for s in schedule)


async def test_a_schedule_that_goes_backwards_is_refused(
    client, api_prefix, auth_headers, admin, route, towns
):
    """Day nine before day eight would make `next_stop` pick the wrong town and
    send every subsequent deviation alert to it."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    start = datetime.now(UTC) + timedelta(hours=4)
    response = await client.put(
        f"{api_prefix}/dindis/{body['id']}/schedule",
        json={
            "stops": [
                {"halt_town_id": str(towns[0].id), "planned_arrival": start.isoformat()},
                {
                    "halt_town_id": str(towns[1].id),
                    "planned_arrival": (start - timedelta(hours=2)).isoformat(),
                },
            ]
        },
        headers=auth_headers(token_for(admin)),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCHEDULE_INVALID"


async def test_the_same_town_twice_is_refused(
    client, api_prefix, auth_headers, admin, route, towns
):
    body = await register(client, api_prefix, auth_headers, admin, route)
    start = datetime.now(UTC) + timedelta(hours=4)
    response = await client.put(
        f"{api_prefix}/dindis/{body['id']}/schedule",
        json={
            "stops": [
                {"halt_town_id": str(towns[0].id), "planned_arrival": start.isoformat()},
                {
                    "halt_town_id": str(towns[0].id),
                    "planned_arrival": (start + timedelta(hours=2)).isoformat(),
                },
            ]
        },
        headers=auth_headers(token_for(admin)),
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# pings
# ---------------------------------------------------------------------------
async def test_a_ping_projects_onto_the_route_and_sets_the_next_interval(
    client, api_prefix, auth_headers, admin, volunteer, route, towns
):
    body = await register(client, api_prefix, auth_headers, admin, route)
    await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    response = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-a",
        json={"lon": ROUTE_START_LON + 0.10, "lat": ROUTE_LAT, "battery": 90},
        headers=auth_headers(token_for(volunteer)),
    )

    assert response.status_code == 200, response.text
    ack = response.json()
    assert ack["accepted"] is True
    assert ack["route_fraction"] == pytest.approx(0.10, abs=0.01)
    assert ack["off_route_m"] == pytest.approx(0.0, abs=50)
    assert ack["next_ping_seconds"] == 60
    assert ack["status"] == "walking"


async def test_a_flat_battery_backs_the_reporting_interval_off(
    client, api_prefix, auth_headers, admin, volunteer, route, towns
):
    """A phone reporting every 60s until it dies on day eleven tells the halt
    towns nothing for the remaining seven days."""
    body = await register(client, api_prefix, auth_headers, admin, route)

    response = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-a",
        json={"lon": ROUTE_START_LON + 0.10, "lat": ROUTE_LAT, "battery": 8},
        headers=auth_headers(token_for(volunteer)),
    )
    assert response.json()["next_ping_seconds"] >= 900


async def test_a_second_phone_cannot_report_for_the_same_dindi(
    client, api_prefix, auth_headers, admin, volunteer, route, towns
):
    """Section 4/M8 designates one device per Dindi. Two reporting phones puts
    the Palkhi in two places on a board halt towns plan against."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    headers = auth_headers(token_for(volunteer))
    position = {"lon": ROUTE_START_LON + 0.10, "lat": ROUTE_LAT}

    first = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-a", json=position, headers=headers
    )
    assert first.status_code == 200

    second = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-b", json=position, headers=headers
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DINDI_DEVICE_MISMATCH"


async def test_a_pilgrim_cannot_report_a_position(
    client, api_prefix, auth_headers, admin, pilgrim, route
):
    body = await register(client, api_prefix, auth_headers, admin, route)
    response = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping",
        json={"lon": ROUTE_START_LON, "lat": ROUTE_LAT},
        headers=auth_headers(token_for(pilgrim)),
    )
    assert response.status_code == 403


async def test_walking_into_a_halt_town_records_the_arrival(
    client, api_prefix, auth_headers, admin, volunteer, route, towns, session
):
    """The gap between planned and actual, across eighteen days and forty
    Dindis, is what next year's schedule gets built from."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    # Saswad sits at 25% along the line.
    saswad_lon = ROUTE_START_LON + 0.25 * (ROUTE_END_LON - ROUTE_START_LON)
    response = await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-a",
        json={"lon": saswad_lon, "lat": ROUTE_LAT, "battery": 70},
        headers=auth_headers(token_for(volunteer)),
    )

    assert response.status_code == 200, response.text
    ack = response.json()
    assert ack["arrived_at"] == "Saswad"
    assert ack["status"] == "halted"
    assert "Saswad" in ack["summary"]

    stop = await session.scalar(
        select(DindiScheduleStop).where(
            DindiScheduleStop.dindi_id == uuid.UUID(body["id"]),
            DindiScheduleStop.halt_town_id == towns[0].id,
        )
    )
    await session.refresh(stop)
    assert stop is not None
    assert stop.actual_arrival is not None


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
async def test_a_dindi_with_no_pings_has_no_eta(
    client, api_prefix, auth_headers, admin, pilgrim, route, towns
):
    """Never a default walking speed dressed up as a measurement."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    response = await client.get(
        f"{api_prefix}/dindis/{body['id']}", headers=auth_headers(token_for(pilgrim))
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["eta"] is None
    assert detail["deviation_minutes"] is None
    assert detail["next_town"] == "Saswad"


async def test_the_list_never_carries_a_leader_phone_number(
    client, api_prefix, auth_headers, admin, pilgrim, route
):
    await register(client, api_prefix, auth_headers, admin, route)
    response = await client.get(f"{api_prefix}/dindis", headers=auth_headers(token_for(pilgrim)))

    assert response.status_code == 200
    assert "9822012345" not in response.text
    assert "leader_phone" not in response.text


async def test_the_list_reports_how_many_groups_it_cannot_see(
    client, api_prefix, auth_headers, admin, volunteer, route
):
    """Fourteen dots on a map means nothing without "and six more are walking
    that we cannot see"."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    await client.post(
        f"{api_prefix}/dindis/{body['id']}/ping?device=phone-a",
        json={"lon": ROUTE_START_LON + 0.1, "lat": ROUTE_LAT},
        headers=auth_headers(token_for(volunteer)),
    )

    response = await client.get(f"{api_prefix}/dindis", headers=auth_headers(token_for(volunteer)))
    body = response.json()
    assert body["reporting"] == 1
    assert body["silent"] == 0


# ---------------------------------------------------------------------------
# the leader's number
# ---------------------------------------------------------------------------
async def test_reading_the_leader_contact_returns_it_and_writes_an_audit_row(
    client, api_prefix, auth_headers, admin, route, session
):
    """Two M8 rules end in "call the Dindi leader", so this has to exist. What
    it can be is never read without a record of who read it."""
    body = await register(client, api_prefix, auth_headers, admin, route)

    response = await client.get(
        f"{api_prefix}/dindis/{body['id']}/leader-contact",
        headers=auth_headers(token_for(admin)),
    )
    assert response.status_code == 200, response.text
    assert response.json()["leader_phone"].endswith("9822012345")

    entries = list(
        (
            await session.execute(
                select(AuditLog).where(AuditLog.target_type == "dindi")
            )
        ).scalars()
    )
    assert any(e.meta.get("read") == "leader_contact" for e in entries)


async def test_a_pilgrim_cannot_read_the_leader_contact(
    client, api_prefix, auth_headers, admin, pilgrim, route
):
    body = await register(client, api_prefix, auth_headers, admin, route)
    response = await client.get(
        f"{api_prefix}/dindis/{body['id']}/leader-contact",
        headers=auth_headers(token_for(pilgrim)),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# the readiness board
# ---------------------------------------------------------------------------
async def test_the_board_sums_head_counts_from_the_schedule(
    client, api_prefix, auth_headers, admin, pilgrim, route, towns
):
    """Not from the planning column somebody typed in months ago — the two
    drift apart the moment a Dindi withdraws."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    response = await client.get(
        f"{api_prefix}/halt-towns?within_hours=72", headers=auth_headers(token_for(pilgrim))
    )
    assert response.status_code == 200, response.text
    saswad = next(t for t in response.json()["towns"] if t["name"] == "Saswad")

    assert saswad["readiness"]["expected_headcount"] == 400
    assert [a["code"] for a in saswad["arriving"]] == ["DND-014"]
    # 400 walkers: 2 water points (4/1000, rounded up), 4 sanitation, 1 medical.
    assert saswad["readiness"]["water_points_required"] == 2
    assert saswad["readiness"]["medical_camps_required"] == 1


async def test_a_town_claiming_ready_without_provisioning_is_flagged(
    client, api_prefix, auth_headers, admin, volunteer, pilgrim, route, towns
):
    """The most useful field on the board."""
    body = await register(client, api_prefix, auth_headers, admin, route)
    await set_schedule(client, api_prefix, auth_headers, admin, body["id"], towns)

    claimed = await client.patch(
        f"{api_prefix}/halt-towns/{towns[0].id}/readiness",
        json={"readiness_status": "ready", "water_points": 0, "medical_camps": 0},
        headers=auth_headers(token_for(volunteer)),
    )
    assert claimed.status_code == 200, claimed.text

    board = await client.get(
        f"{api_prefix}/halt-towns?within_hours=72", headers=auth_headers(token_for(pilgrim))
    )
    saswad = next(t for t in board.json()["towns"] if t["name"] == "Saswad")

    assert saswad["readiness"]["declared"] == "ready"
    assert saswad["readiness"]["computed"] == "not_ready"
    assert saswad["readiness"]["disagrees"] is True
    assert saswad["readiness"]["gaps"]


async def test_a_volunteer_can_update_readiness_and_it_is_stamped(
    client, api_prefix, auth_headers, volunteer, towns, session
):
    """The person who can say how many tankers are in Saswad is standing in
    Saswad. Routing this through an administrator is how the board goes stale.
    """
    response = await client.patch(
        f"{api_prefix}/halt-towns/{towns[0].id}/readiness",
        json={"water_points": 4, "sanitation_units": 10, "medical_camps": 1, "readiness_status": "ready"},
        headers=auth_headers(token_for(volunteer)),
    )
    assert response.status_code == 200, response.text
    assert response.json()["readiness_updated_at"] is not None

    town = await session.get(HaltTown, towns[0].id)
    await session.refresh(town)
    assert town.readiness_updated_by == volunteer.id
    assert town.water_points == 4


async def test_readiness_updates_are_audited(
    client, api_prefix, auth_headers, volunteer, towns, session
):
    await client.patch(
        f"{api_prefix}/halt-towns/{towns[0].id}/readiness",
        json={"water_points": 6},
        headers=auth_headers(token_for(volunteer)),
    )
    entries = list(
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "halt_town.readiness_updated")
            )
        ).scalars()
    )
    assert len(entries) == 1
    assert entries[0].meta["before"]["water_points"] == 0
    assert entries[0].meta["after"]["water_points"] == 6


async def test_a_town_with_nobody_due_reads_as_unknown_not_ready(
    client, api_prefix, auth_headers, pilgrim, towns
):
    response = await client.get(f"{api_prefix}/halt-towns", headers=auth_headers(token_for(pilgrim)))
    lonand = next(t for t in response.json()["towns"] if t["name"] == "Lonand")

    assert lonand["readiness"]["expected_headcount"] == 0
    assert lonand["readiness"]["computed"] == "unknown"


async def test_the_board_states_the_ratios_it_used(
    client, api_prefix, auth_headers, pilgrim, towns
):
    """A changed planning convention shows up in the output rather than hiding
    behind it."""
    response = await client.get(f"{api_prefix}/halt-towns", headers=auth_headers(token_for(pilgrim)))
    town = response.json()["towns"][0]

    assert "per 1000 walkers" in town["readiness"]["basis"]
    assert town["readiness"]["basis_mr"]
