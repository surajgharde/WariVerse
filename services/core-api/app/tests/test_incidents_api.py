"""End-to-end Phase 5: SOS -> triage -> dispatch -> resolve -> close.

The behaviours pinned here are the ones Section 4/M4 states as rules rather
than as features, because those are the ones that quietly stop being true:

* an SOS is never refused, at any press count;
* nothing is auto-dispatched;
* closing an incident requires saying what was done;
* a volunteer may work the small stuff and cannot re-grade their way past that;
* re-grading does not hand a late incident a fresh clock;
* a pilgrim can read their own emergency and no one else's.

Needs Postgres and Redis (`docker compose up -d db redis`).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import Role
from app.core.security import create_access_token, now_utc
from app.models import Incident, MissingPerson, Responder, Zone
from app.models.incidents import IncidentSeverity, IncidentStatus
from app.models.user import ContactSecret
from app.services import incident_service

pytestmark = [pytest.mark.db, pytest.mark.redis]

#: Two points about 100 m apart inside the zone polygon below.
NEAR = [75.3300, 17.6790]
FAR = [75.3310, 17.6790]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reload_incident(session: AsyncSession, incident_id: str) -> Incident:
    """Fetch the row behind a JSON id, so a test can age it or read it raw."""
    incident = await session.get(Incident, uuid.UUID(incident_id))
    assert incident is not None, f"incident {incident_id} vanished between the API call and the read"
    return incident


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
async def ambulance(session: AsyncSession) -> Responder:
    record = Responder(
        call_sign="AMB-1",
        unit_type="ambulance",
        status="available",
        current_location=f"SRID=4326;POINT({NEAR[0]} {NEAR[1]})",
        last_ping_at=now_utc(),
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def squad(session: AsyncSession) -> Responder:
    record = Responder(
        call_sign="VOL-1",
        unit_type="volunteer_squad",
        status="available",
        current_location=f"SRID=4326;POINT({NEAR[0]} {NEAR[1]})",
        last_ping_at=now_utc(),
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


@pytest.fixture
async def officer_token(make_user):
    user = await make_user(phone="9000000003", role=Role.SECURITY_OFFICER, name="सुरक्षा अधिकारी")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


async def open_incident(
    client: AsyncClient,
    api_prefix: str,
    token: str,
    zone: Zone,
    *,
    severity: str = "normal",
    incident_type: str = "facility_failure",
) -> dict:
    response = await client.post(
        f"{api_prefix}/incidents",
        json={
            "type": incident_type,
            "severity": severity,
            "zone_id": str(zone.id),
            "description": "Test incident",
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# SOS — the button that is never refused
# ---------------------------------------------------------------------------
async def test_an_sos_comes_back_with_a_reference_and_a_marathi_line(
    client: AsyncClient, api_prefix: str, zone: Zone, pilgrim_token: str
):
    """Section 4/M4: "Never leave them staring at a spinner." """
    response = await client.post(
        f"{api_prefix}/sos",
        json={"location": NEAR, "zone_id": str(zone.id), "type": "medical"},
        headers=bearer(pilgrim_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reference"].startswith("INC-")
    assert body["message"] and body["message_mr"]
    assert body["status"] == "reported"
    # No unit has been assigned, and the response says so rather than leaving a
    # null field to speak for itself.
    assert body["responder_eta_seconds"] is None
    assert body["joined_existing"] is False


async def test_every_sos_is_graded_critical_whatever_the_client_asked_for(
    client: AsyncClient, api_prefix: str, zone: Zone, pilgrim_token: str, officer_token: str
):
    """A pilgrim is not triaging themselves, and the three-minute clock is the
    whole point of the button."""
    await client.post(
        f"{api_prefix}/sos",
        json={"location": NEAR, "type": "lost_item"},
        headers=bearer(pilgrim_token),
    )

    listed = await client.get(f"{api_prefix}/incidents", headers=bearer(officer_token))
    incident = listed.json()["items"][0]
    assert incident["severity"] == "critical"
    assert incident["source"] == "pilgrim_sos"


async def test_the_fourth_press_joins_the_open_sos_instead_of_being_refused(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, officer_token: str
):
    """Section 9 sets a limit of 3 per 10 minutes and then says never hard-block
    an SOS. Over the limit the call does not fail — it attaches.

    Note where the line actually falls: the limit is 3, so the first three
    presses each open their own incident and only the fourth is absorbed, onto
    the *most recent* of them. Every press returns 201 with a usable reference,
    which is the property that matters to the person holding the phone.
    """
    references = []
    for _ in range(settings.rate_limit_sos_per_10min + 1):
        response = await client.post(
            f"{api_prefix}/sos", json={"location": NEAR}, headers=bearer(pilgrim_token)
        )
        assert response.status_code == 201, response.text
        references.append(response.json())

    assert all(r["reference"] for r in references)
    assert [r["joined_existing"] for r in references] == [False, False, False, True]
    # The fourth attaches to the third rather than opening a fourth row.
    assert references[-1]["reference"] == references[-2]["reference"]

    listed = await client.get(f"{api_prefix}/incidents", headers=bearer(officer_token))
    assert listed.json()["total"] == settings.rate_limit_sos_per_10min

    timeline = await client.get(
        f"{api_prefix}/incidents/{references[-1]['incident_id']}", headers=bearer(officer_token)
    )
    events = timeline.json()["timeline"]
    repeat = next(e for e in events if e["action"] == "sos_repeated")
    assert repeat["meta"]["press_count"] == settings.rate_limit_sos_per_10min + 1


async def test_an_sos_naming_an_unknown_zone_is_still_taken(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, officer_token: str
):
    """A client working off a stale offline bundle must not have its emergency
    rejected over a zone id."""
    response = await client.post(
        f"{api_prefix}/sos",
        json={"location": NEAR, "zone_id": str(uuid.uuid4())},
        headers=bearer(pilgrim_token),
    )

    assert response.status_code == 201, response.text
    detail = await client.get(
        f"{api_prefix}/incidents/{response.json()['incident_id']}", headers=bearer(officer_token)
    )
    body = detail.json()
    assert body["zone_id"] is None
    assert "zone_unknown" in [event["action"] for event in body["timeline"]]


async def test_an_sos_queued_offline_keeps_the_clock_it_started(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, officer_token: str
):
    """An SOS pressed twenty minutes ago in a dead spot has already used twenty
    minutes of its three-minute SLA. The console must show that, not a fresh
    clock — the delay is the most important fact about a late-arriving
    emergency."""
    pressed_at = now_utc() - timedelta(minutes=20)
    response = await client.post(
        f"{api_prefix}/sos",
        json={"location": NEAR, "client_reported_at": pressed_at.isoformat()},
        headers=bearer(pilgrim_token),
    )

    detail = await client.get(
        f"{api_prefix}/incidents/{response.json()['incident_id']}", headers=bearer(officer_token)
    )
    body = detail.json()
    assert body["delayed_by_seconds"] == pytest.approx(1200, abs=60)
    # Three-minute SLA measured from a twenty-minute-old press: already overdue.
    assert body["seconds_to_sla"] < 0
    assert body["seconds_open"] == pytest.approx(1200, abs=60)


async def test_a_client_clock_running_fast_cannot_buy_extra_sla(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, officer_token: str
):
    future = now_utc() + timedelta(hours=2)
    response = await client.post(
        f"{api_prefix}/sos",
        json={"location": NEAR, "client_reported_at": future.isoformat()},
        headers=bearer(pilgrim_token),
    )

    detail = await client.get(
        f"{api_prefix}/incidents/{response.json()['incident_id']}", headers=bearer(officer_token)
    )
    # Clamped to now: three minutes of SLA, not two hours and three minutes.
    assert detail.json()["seconds_to_sla"] <= 180


async def test_a_pilgrim_reads_their_own_sos_and_no_one_elses(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, make_user
):
    raised = await client.post(
        f"{api_prefix}/sos", json={"location": NEAR}, headers=bearer(pilgrim_token)
    )
    reference = raised.json()["reference"]

    mine = await client.get(f"{api_prefix}/sos/{reference}", headers=bearer(pilgrim_token))
    assert mine.status_code == 200
    assert mine.json()["reference"] == reference

    other = await make_user(phone="9111111111", role=Role.PILGRIM, password=None)
    other_token, _ = create_access_token(subject=str(other.id), role=other.role)
    theirs = await client.get(f"{api_prefix}/sos/{reference}", headers=bearer(other_token))

    # A real reference belonging to someone else answers exactly as a fake one
    # does. The 404 must not become a way to confirm a code is real.
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


async def test_a_pilgrim_cannot_list_the_incident_board(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
):
    """"Who needed help at the Wari and where" is not a list this system hands
    out."""
    response = await client.get(f"{api_prefix}/incidents", headers=bearer(pilgrim_token))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
async def test_a_client_cannot_name_its_own_provenance(
    client: AsyncClient, api_prefix: str, zone: Zone, pilgrim_token: str, officer_token: str
):
    """A pilgrim claiming `ai_alert` gets `pilgrim_report`.

    Not bookkeeping: `_open_sos_for` finds a caller's open SOS by
    `source == "pilgrim_sos"`, so letting a hand-filed report claim that source
    would make the pilgrim's next panic press attach itself to a lost umbrella.
    """
    response = await client.post(
        f"{api_prefix}/incidents",
        json={
            "type": "lost_item",
            "severity": "low",
            "source": "ai_alert",
            "zone_id": str(zone.id),
        },
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 201, response.text

    detail = await client.get(
        f"{api_prefix}/incidents/{response.json()['id']}", headers=bearer(officer_token)
    )
    assert detail.json()["source"] == "pilgrim_report"


async def test_an_operator_may_log_a_phone_call(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    response = await client.post(
        f"{api_prefix}/incidents",
        json={
            "type": "medical",
            "severity": "high",
            "source": "phone_call",
            "zone_id": str(zone.id),
            "contact_phone": "9822012345",
        },
        headers=bearer(officer_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "phone_call"


async def test_a_callback_number_is_hashed_on_the_row_and_encrypted_elsewhere(
    client: AsyncClient, api_prefix: str, session: AsyncSession, zone: Zone, officer_token: str
):
    """Section 12 data minimisation, applied to incidents the way Phase 2
    applied it to passes. The raw number appears nowhere on the incident."""
    await client.post(
        f"{api_prefix}/incidents",
        json={
            "type": "medical",
            "severity": "high",
            "source": "phone_call",
            "zone_id": str(zone.id),
            "contact_phone": "9822012345",
        },
        headers=bearer(officer_token),
    )

    incident = (await session.execute(select(Incident))).scalars().one()
    assert incident.reporter_phone_hash is not None
    assert "9822012345" not in (incident.reporter_phone_hash or "")
    assert "9822012345" not in (incident.description or "")

    secret = (await session.execute(select(ContactSecret))).scalars().one()
    assert secret.purpose == "incident_contact"
    assert "9822012345" not in secret.encrypted_phone
    assert secret.purge_after is not None


async def test_an_incident_nobody_can_find_is_refused(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    """"Somewhere in the temple" is not a dispatchable fact."""
    response = await client.post(
        f"{api_prefix}/incidents",
        json={"type": "medical", "severity": "high"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# the lifecycle
# ---------------------------------------------------------------------------
async def test_the_happy_path_runs_end_to_end(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    incident = await open_incident(
        client, api_prefix, officer_token, zone, severity="critical", incident_type="medical"
    )
    incident_id = incident["id"]

    triaged = await client.patch(
        f"{api_prefix}/incidents/{incident_id}",
        json={"status": "triaged", "note": "Confirmed by radio"},
        headers=bearer(officer_token),
    )
    assert triaged.status_code == 200
    assert triaged.json()["status"] == "triaged"

    dispatched = await client.post(
        f"{api_prefix}/incidents/{incident_id}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["status"] == "dispatched"
    assert dispatched.json()["assigned_call_sign"] == "AMB-1"
    assert dispatched.json()["first_response_at"] is not None

    for status in ("on_scene", "resolved"):
        moved = await client.patch(
            f"{api_prefix}/incidents/{incident_id}",
            json={"status": status},
            headers=bearer(officer_token),
        )
        assert moved.status_code == 200, moved.text

    closed = await client.patch(
        f"{api_prefix}/incidents/{incident_id}",
        json={"status": "closed", "outcome_note": "Treated on site, walked away."},
        headers=bearer(officer_token),
    )
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "closed"
    assert body["outcome_note"] == "Treated on site, walked away."

    # The timeline is the record: every step took a line.
    detail = await client.get(f"{api_prefix}/incidents/{incident_id}", headers=bearer(officer_token))
    actions = [event["action"] for event in detail.json()["timeline"]]
    assert actions[0] == "reported"
    assert "dispatched" in actions
    assert "status:closed" in actions


async def test_closing_without_saying_what_was_done_is_refused(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """A closed incident with no record of what was done is the one an inquiry
    will ask about."""
    incident = await open_incident(client, api_prefix, officer_token, zone)
    await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=bearer(officer_token),
    )

    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "closed"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OUTCOME_NOTE_REQUIRED"


async def test_an_illegal_transition_names_what_was_allowed(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    incident = await open_incident(client, api_prefix, officer_token, zone)

    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "closed"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "INVALID_TRANSITION"
    # The error tells the operator where they can actually go from here.
    assert "triaged" in body["details"]["allowed"]


async def test_a_false_alarm_can_be_resolved_without_being_dragged_through_dispatch(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """Something an operator handled over the radio in twenty seconds should not
    have to be walked through triage and dispatch to be closed honestly."""
    incident = await open_incident(client, api_prefix, officer_token, zone)
    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved", "note": "False alarm"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


async def test_regrading_does_not_hand_a_late_incident_a_fresh_clock(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    officer_token: str,
):
    """Otherwise re-grading becomes a way to make a late response look punctual —
    which is exactly the number an inquiry would check."""
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="normal")

    # Age it: reported twenty minutes ago, so a critical clock is long gone.
    row = await reload_incident(session, incident["id"])
    row.created_at = now_utc() - timedelta(minutes=20)
    await session.commit()

    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"severity": "critical", "note": "Worse than first reported"},
        headers=bearer(officer_token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["severity"] == "critical"
    # Three minutes from a twenty-minute-old report is seventeen minutes ago.
    assert body["seconds_to_sla"] < 0


# ---------------------------------------------------------------------------
# who may do what
# ---------------------------------------------------------------------------
async def test_a_volunteer_may_close_the_small_stuff(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str, volunteer_token: str
):
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="low")
    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved", "note": "Umbrella returned"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 200, response.text


async def test_a_volunteer_may_not_touch_a_critical_incident(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str, volunteer_token: str
):
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="critical")
    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["severity"] == "critical"


async def test_a_volunteer_cannot_regrade_their_way_past_the_guard(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str, volunteer_token: str
):
    """The hole this closes: downgrade a critical to low, then close it through
    the low-severity door."""
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="critical")
    response = await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"severity": "low"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 403
    assert "incident:update_any" in response.json()["error"]["details"]["missing_permissions"]


async def test_a_volunteer_cannot_dispatch(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
    volunteer_token: str,
):
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="low")
    response = await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
async def test_suggestions_rank_by_type_then_distance_and_dispatch_nothing(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    squad: Responder,
    officer_token: str,
):
    """Section 4/M4: "Auto-suggest ... but a human confirms. No auto-dispatch." """
    incident = await open_incident(
        client, api_prefix, officer_token, zone, severity="critical", incident_type="medical"
    )

    response = await client.get(
        f"{api_prefix}/incidents/{incident['id']}/dispatch-options", headers=bearer(officer_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["call_sign"] for s in body["suggestions"]] == ["AMB-1", "VOL-1"]
    assert body["available_units"] == 2
    assert body["note"] and body["note_mr"]

    # Asking for options changed nothing.
    unchanged = await client.get(
        f"{api_prefix}/incidents/{incident['id']}", headers=bearer(officer_token)
    )
    assert unchanged.json()["status"] == "reported"
    assert unchanged.json()["assigned_responder_id"] is None


async def test_an_empty_suggestion_list_says_why(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """An empty list must never be read as "no units exist" — those are
    different situations and an operator reaching for the radio needs to know
    which one they are in."""
    incident = await open_incident(client, api_prefix, officer_token, zone)
    response = await client.get(
        f"{api_prefix}/incidents/{incident['id']}/dispatch-options", headers=bearer(officer_token)
    )
    body = response.json()
    assert body["suggestions"] == []
    assert body["available_units"] == 0
    assert "No unit is available" in body["note"]


async def test_dispatching_a_busy_unit_is_refused(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    first = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")
    second = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")

    ok = await client.post(
        f"{api_prefix}/incidents/{first['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )
    assert ok.status_code == 200

    clash = await client.post(
        f"{api_prefix}/incidents/{second['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "RESPONDER_UNAVAILABLE"


async def test_reconfirming_the_same_unit_is_not_an_error(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    """An operator re-confirming under pressure should not see a failure."""
    incident = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")
    for _ in range(2):
        response = await client.post(
            f"{api_prefix}/incidents/{incident['id']}/dispatch",
            json={"responder_id": str(ambulance.id)},
            headers=bearer(officer_token),
        )
        assert response.status_code == 200, response.text


async def test_resolving_an_incident_puts_its_unit_back_on_the_board(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    incident = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")
    await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )
    await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=bearer(officer_token),
    )

    await session.refresh(ambulance)
    assert ambulance.status == "available"


async def test_an_override_is_allowed_and_recorded(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    squad: Responder,
    officer_token: str,
):
    """The person on the radio knows things the map does not. Not a blocker —
    but a pattern of overrides is worth seeing."""
    incident = await open_incident(
        client, api_prefix, officer_token, zone, severity="critical", incident_type="medical"
    )
    response = await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={
            "responder_id": str(squad.id),
            "override_reason": "Ambulance cannot reach the gate — barricades up",
        },
        headers=bearer(officer_token),
    )
    assert response.status_code == 200, response.text

    detail = await client.get(f"{api_prefix}/incidents/{incident['id']}", headers=bearer(officer_token))
    dispatched = next(e for e in detail.json()["timeline"] if e["action"] == "dispatched")
    assert dispatched["meta"]["override_reason"].startswith("Ambulance cannot reach")


async def test_dispatching_a_closed_incident_is_refused(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    incident = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")
    await client.patch(
        f"{api_prefix}/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=bearer(officer_token),
    )

    response = await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INCIDENT_CLOSED"


# ---------------------------------------------------------------------------
# responders
# ---------------------------------------------------------------------------
async def test_the_roster_carries_position_age_on_every_row(
    client: AsyncClient, api_prefix: str, ambulance: Responder, officer_token: str
):
    """Not only on the stale ones: a board that flags staleness past a threshold
    teaches an operator that an unflagged dot is current."""
    response = await client.get(f"{api_prefix}/responders", headers=bearer(officer_token))
    assert response.status_code == 200
    row = response.json()[0]
    assert row["call_sign"] == "AMB-1"
    assert row["seconds_since_ping"] is not None
    assert row["location"] == pytest.approx(NEAR)


async def test_a_unit_cannot_free_itself_while_an_incident_is_still_open(
    client: AsyncClient,
    api_prefix: str,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    """That would take it off the board with the incident still assigned, and
    the next dispatch would double-book a unit already in use."""
    incident = await open_incident(client, api_prefix, officer_token, zone, incident_type="medical")
    await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )

    response = await client.post(
        f"{api_prefix}/responders/{ambulance.id}/ping",
        json={"location": FAR, "status": "available"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_a_ping_moves_the_unit_on_the_map(
    client: AsyncClient, api_prefix: str, ambulance: Responder, officer_token: str
):
    response = await client.post(
        f"{api_prefix}/responders/{ambulance.id}/ping",
        json={"location": FAR},
        headers=bearer(officer_token),
    )
    assert response.status_code == 200
    assert response.json()["location"] == pytest.approx(FAR)
    assert response.json()["seconds_since_ping"] == 0.0


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------
async def test_an_unanswered_incident_breaches_and_says_by_how_much(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    officer_token: str,
):
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="critical")

    row = await reload_incident(session, incident["id"])
    row.sla_due_at = now_utc() - timedelta(minutes=5)
    await session.commit()

    sweep = await client.post(f"{api_prefix}/admin/incidents/sla-sweep", headers=bearer(officer_token))
    assert sweep.status_code == 200, sweep.text
    assert sweep.json()["breached"] == 1

    detail = await client.get(f"{api_prefix}/incidents/{incident['id']}", headers=bearer(officer_token))
    assert detail.json()["sla_breached"] is True
    breach = next(e for e in detail.json()["timeline"] if e["action"] == "sla_breached")
    assert breach["meta"]["overdue_seconds"] == pytest.approx(300, abs=30)


async def test_an_incident_a_unit_is_already_on_cannot_breach(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    ambulance: Responder,
    officer_token: str,
):
    """Once a unit is on the way the control room has done the thing the SLA
    measures. Marking it a control-room failure would push operators to dispatch
    anybody just to stop the clock."""
    incident = await open_incident(
        client, api_prefix, officer_token, zone, severity="critical", incident_type="medical"
    )
    await client.post(
        f"{api_prefix}/incidents/{incident['id']}/dispatch",
        json={"responder_id": str(ambulance.id)},
        headers=bearer(officer_token),
    )

    row = await reload_incident(session, incident["id"])
    row.sla_due_at = now_utc() - timedelta(minutes=5)
    await session.commit()

    sweep = await client.post(f"{api_prefix}/admin/incidents/sla-sweep", headers=bearer(officer_token))
    assert sweep.json()["breached"] == 0


async def test_the_sweep_is_idempotent(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    officer_token: str,
):
    """Two replicas racing must not write two breach lines onto one timeline."""
    incident = await open_incident(client, api_prefix, officer_token, zone, severity="critical")
    row = await reload_incident(session, incident["id"])
    row.sla_due_at = now_utc() - timedelta(minutes=5)
    await session.commit()

    first = await client.post(f"{api_prefix}/admin/incidents/sla-sweep", headers=bearer(officer_token))
    second = await client.post(f"{api_prefix}/admin/incidents/sla-sweep", headers=bearer(officer_token))
    assert first.json()["breached"] == 1
    assert second.json()["breached"] == 0

    detail = await client.get(f"{api_prefix}/incidents/{incident['id']}", headers=bearer(officer_token))
    breaches = [e for e in detail.json()["timeline"] if e["action"] == "sla_breached"]
    assert len(breaches) == 1


async def test_the_board_lists_worst_first_then_least_time_left(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    await open_incident(client, api_prefix, officer_token, zone, severity="low")
    await open_incident(client, api_prefix, officer_token, zone, severity="critical")
    await open_incident(client, api_prefix, officer_token, zone, severity="normal")

    response = await client.get(f"{api_prefix}/incidents", headers=bearer(officer_token))
    severities = [item["severity"] for item in response.json()["items"]]
    assert severities == ["critical", "normal", "low"]


# ---------------------------------------------------------------------------
# missing persons
# ---------------------------------------------------------------------------
async def test_a_missing_person_case_opens_an_incident_on_the_same_board(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """A missing child that lives in its own list is a missing child nobody is
    assigned to."""
    response = await client.post(
        f"{api_prefix}/missing-persons",
        json={
            "name": "आरती जाधव",
            "age": 7,
            "last_seen_zone_id": str(zone.id),
            "contact_phone": "9822012345",
            "language": "mr",
            "photo_uri": "s3://evidence/mp/1.jpg",
        },
        headers=bearer(officer_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["incident_reference"].startswith("INC-")
    assert body["status"] == "open"
    # Whether a photo exists, not where it is.
    assert body["has_photo"] is True
    assert "photo_uri" not in body

    board = await client.get(f"{api_prefix}/incidents", headers=bearer(officer_token))
    incident = board.json()["items"][0]
    assert incident["type"] == "missing_person"
    # High, not critical: a search organised properly must not queue ahead of
    # the cardiac arrest.
    assert incident["severity"] == "high"


async def test_the_callback_number_never_reaches_the_incident_board(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The name is on the board on purpose — an operator organising a search
    needs it. The number to call when she is found is not, and never is.
    """
    await client.post(
        f"{api_prefix}/missing-persons",
        json={"name": "आरती जाधव", "age": 7, "contact_phone": "9822012345"},
        headers=bearer(officer_token),
    )
    board = await client.get(f"{api_prefix}/incidents", headers=bearer(officer_token))
    assert "9822012345" not in board.text


async def test_reunification_closes_the_case_and_the_incident(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    officer_token: str,
):
    """A case that ends without the incident ending leaves an open row on the
    board for something that is finished."""
    opened = await client.post(
        f"{api_prefix}/missing-persons",
        json={"name": "आरती जाधव", "age": 7, "contact_phone": "9822012345"},
        headers=bearer(officer_token),
    )
    case_id = opened.json()["id"]

    response = await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "reunited", "note": "Found at the help desk with her uncle."},
        headers=bearer(officer_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reunited"
    assert body["resolved_at"] is not None
    # The purge clock starts at closure, not at report.
    assert body["purge_after"] is not None

    incident = await reload_incident(session, opened.json()["incident_id"])
    await session.refresh(incident)
    assert incident.status == str(IncidentStatus.RESOLVED)
    assert incident.outcome_note


async def test_a_volunteer_may_report_a_sighting_but_not_end_the_search(
    client: AsyncClient, api_prefix: str, officer_token: str, volunteer_token: str
):
    """Marking a sighting is the whole reason the case is broadcast to them.
    Standing the search down is a claim somebody has to own."""
    opened = await client.post(
        f"{api_prefix}/missing-persons",
        json={"name": "आरती जाधव", "age": 7, "contact_phone": "9822012345"},
        headers=bearer(officer_token),
    )
    case_id = opened.json()["id"]

    sighted = await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "sighted", "note": "Seen near gate 3"},
        headers=bearer(volunteer_token),
    )
    assert sighted.status_code == 200, sighted.text

    reunited = await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "reunited"},
        headers=bearer(volunteer_token),
    )
    assert reunited.status_code == 403


async def test_a_closed_case_cannot_be_reopened_by_a_patch(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    opened = await client.post(
        f"{api_prefix}/missing-persons",
        json={"name": "आरती जाधव", "contact_phone": "9822012345"},
        headers=bearer(officer_token),
    )
    case_id = opened.json()["id"]
    await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "reunited"},
        headers=bearer(officer_token),
    )

    again = await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "open"},
        headers=bearer(officer_token),
    )
    assert again.status_code in (409, 422)


async def test_the_missing_person_list_is_oldest_first(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    """A person missing for two hours is the one the announcement desk works
    next; a newest-first list buries them."""
    for name in ("पहिली", "दुसरी", "तिसरी"):
        await client.post(
            f"{api_prefix}/missing-persons",
            json={"name": name, "contact_phone": "9822012345"},
            headers=bearer(officer_token),
        )

    response = await client.get(f"{api_prefix}/missing-persons", headers=bearer(officer_token))
    names = [item["name"] for item in response.json()["items"]]
    assert names == ["पहिली", "दुसरी", "तिसरी"]


async def test_photos_are_purged_after_retention_and_the_case_survives(
    session: AsyncSession, client: AsyncClient, api_prefix: str, officer_token: str
):
    """Section 12: photos auto-purge 30 days after closure. The case record is
    what the reunification was logged against and stays."""
    opened = await client.post(
        f"{api_prefix}/missing-persons",
        json={
            "name": "आरती जाधव",
            "contact_phone": "9822012345",
            "photo_uri": "s3://evidence/mp/1.jpg",
        },
        headers=bearer(officer_token),
    )
    case_id = opened.json()["id"]
    await client.patch(
        f"{api_prefix}/missing-persons/{case_id}",
        json={"status": "reunited"},
        headers=bearer(officer_token),
    )

    record = await session.get(MissingPerson, uuid.UUID(case_id))
    assert record is not None
    await session.refresh(record)
    assert record.photo_uri is not None

    purged = await incident_service.purge_missing_person_photos(
        session, at=now_utc() + timedelta(days=incident_service.PHOTO_RETENTION_DAYS + 1)
    )
    await session.commit()

    assert len(purged) == 1
    await session.refresh(record)
    assert record.photo_uri is None
    assert record.status == "reunited"


async def test_an_open_case_past_thirty_days_keeps_its_photo(
    session: AsyncSession, client: AsyncClient, api_prefix: str, officer_token: str
):
    """Retention is measured from closure, not from report. A case still open on
    day 31 has not stopped needing the photo."""
    await client.post(
        f"{api_prefix}/missing-persons",
        json={
            "name": "आरती जाधव",
            "contact_phone": "9822012345",
            "photo_uri": "s3://evidence/mp/1.jpg",
        },
        headers=bearer(officer_token),
    )

    purged = await incident_service.purge_missing_person_photos(
        session, at=now_utc() + timedelta(days=365)
    )
    assert purged == []


# ---------------------------------------------------------------------------
# the command centre KPI this phase turns on
# ---------------------------------------------------------------------------
async def test_open_incidents_stops_being_unavailable(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """Phase 4 shipped this card as structurally unknown. Phase 5 gives it a
    real count — and an empty board now honestly reads zero."""
    empty = await client.get(f"{api_prefix}/command/kpis", headers=bearer(officer_token))
    card = next(k for k in empty.json()["kpis"] if k["key"] == "open_incidents")
    assert card["value"] == 0
    assert card["source"] == "live"
    assert card["state"] == "ok"

    await open_incident(client, api_prefix, officer_token, zone, severity=str(IncidentSeverity.CRITICAL))

    busy = await client.get(f"{api_prefix}/command/kpis", headers=bearer(officer_token))
    card = next(k for k in busy.json()["kpis"] if k["key"] == "open_incidents")
    assert card["value"] == 1
    # State is driven by criticals and breaches, not by volume.
    assert card["state"] == "watch"
    assert card["detail"]["critical"] == 1


async def test_the_kpi_state_escalates_on_a_breach_not_on_volume(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    zone: Zone,
    officer_token: str,
):
    """Twelve open lost-item reports is a busy help desk. One critical nobody
    was sent to is the thing the strip exists to show."""
    for _ in range(6):
        await open_incident(client, api_prefix, officer_token, zone, severity="low")

    quiet = await client.get(f"{api_prefix}/command/kpis", headers=bearer(officer_token))
    card = next(k for k in quiet.json()["kpis"] if k["key"] == "open_incidents")
    assert card["value"] == 6
    assert card["state"] == "ok"

    incident = await open_incident(client, api_prefix, officer_token, zone, severity="critical")
    row = await reload_incident(session, incident["id"])
    row.sla_due_at = now_utc() - timedelta(minutes=5)
    await session.commit()
    await client.post(f"{api_prefix}/admin/incidents/sla-sweep", headers=bearer(officer_token))

    loud = await client.get(f"{api_prefix}/command/kpis", headers=bearer(officer_token))
    card = next(k for k in loud.json()["kpis"] if k["key"] == "open_incidents")
    assert card["state"] == "breach"
    assert "past their SLA" in card["note"]
