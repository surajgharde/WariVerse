"""The breach ledger end to end (Section 4/M5, Phase 6).

Needs Postgres and Redis (`docker compose up -d db redis`).

Section 4/M5 calls this "the most sensitive module in the product". The tests
that matter most here are not the happy paths — they are the ones that prove the
hard constraints hold:

* a pilgrim or volunteer cannot see the ledger at all;
* an event does not count until a human reviews it;
* the chain notices tampering, whether by edit or by deletion;
* an Administrator cannot remove evidence, and a System Admin cannot do it
  silently;
* every clip view is re-authenticated and logged.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, now_utc
from app.models import BreachEvent, Camera, Gate, Pass, Slot, Tripwire, Zone
from app.services import breach_service, config_service

pytestmark = [pytest.mark.db, pytest.mark.redis]

AI_HEADERS = {"x-ai-service-token": "test-ai-service-token"}
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _clean_caches():
    config_service.clear_cache()
    yield
    config_service.clear_cache()


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
        geom="SRID=4326;POLYGON((75.33 17.679, 75.331 17.679, 75.331 17.68, 75.33 17.68, 75.33 17.679))",
        area_m2=1200.0,
        capacity_persons=2400,
        zone_type="temple_core",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def gate(session: AsyncSession, zone: Zone) -> Gate:
    """A restricted gate, flagged closed. Section 4/M5's whole scope."""
    record = Gate(
        code="G3",
        name="VIP / Restricted Side Entry",
        name_mr="विशेष प्रवेश (प्रतिबंधित)",
        zone_id=zone.id,
        location="SRID=4326;POINT(75.3304 17.68)",
        throughput_per_hour=400,
        is_restricted=True,
        is_open=False,
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def camera(session: AsyncSession, zone: Zone) -> Camera:
    record = Camera(
        zone_id=zone.id,
        name="CAM-G3-01",
        status="online",
        last_heartbeat_at=now_utc(),
        is_tripwire_enabled=True,
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def tripwire(session: AsyncSession, camera: Camera, gate: Gate) -> Tripwire:
    record = Tripwire(
        camera_id=camera.id,
        gate_id=gate.id,
        name="G3 restricted line",
        geometry={"points": [[0.2, 0.5], [0.8, 0.5]]},
        restricted_direction="in",
        active_schedule={},
        is_active=True,
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def officer_token(make_user):
    user = await make_user(phone="9000000003", role=Role.SECURITY_OFFICER, name="सुरक्षा अधिकारी")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=False)
    return token


@pytest.fixture
async def volunteer_token(make_user):
    user = await make_user(phone="9000000004", role=Role.VOLUNTEER, name="स्वयंसेवक")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=False)
    return token


@pytest.fixture
async def pilgrim_token(make_user):
    user = await make_user(phone="9876543210", role=Role.PILGRIM, password=None, name="यात्रेकरू")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=False)
    return token


@pytest.fixture
async def admin_token(make_user):
    user = await make_user(phone="9000000002", role=Role.ADMINISTRATOR, name="मंदिर प्रशासक")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=True)
    return token


@pytest.fixture
async def sysadmin_token(make_user):
    user = await make_user(phone="9000000001", role=Role.SYSTEM_ADMIN, name="सिस्टम प्रशासक")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=True)
    return token


def crossing(tripwire: Tripwire, **overrides) -> dict:
    payload = {
        "tripwire_id": str(tripwire.id),
        "occurred_at": now_utc().isoformat(),
        "direction": "in",
        "confidence": 0.87,
        "crossing_count": 1,
        "clip_uri": "s3://evidence/g3/clip-001.mp4",
        "clip_sha256": "a" * 64,
    }
    payload.update(overrides)
    return payload


async def report(client: AsyncClient, api_prefix: str, *crossings: dict) -> dict:
    response = await client.post(
        f"{api_prefix}/ingest/breach", json={"crossings": list(crossings)}, headers=AI_HEADERS
    )
    assert response.status_code == 202, response.text
    return response.json()


# ---------------------------------------------------------------------------
# no public exposure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["breaches", "breaches/verify", "breaches/summary", "tripwires"])
async def test_a_pilgrim_cannot_reach_the_ledger(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, path: str
):
    """Section 4/M5: "never on any pilgrim-facing surface"."""
    response = await client.get(f"{api_prefix}/{path}", headers=bearer(pilgrim_token))
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["breaches", "breaches/verify", "breaches/summary", "tripwires"])
async def test_a_volunteer_cannot_reach_the_ledger(
    client: AsyncClient, api_prefix: str, volunteer_token: str, path: str
):
    """"Visible only to Security Officer and Administrator roles."

    A volunteer holds `incident:view` and works the same crowd — and still
    cannot see who was recorded skipping a queue.
    """
    response = await client.get(f"{api_prefix}/{path}", headers=bearer(volunteer_token))
    assert response.status_code == 403


async def test_the_ledger_requires_a_token(client: AsyncClient, api_prefix: str):
    assert (await client.get(f"{api_prefix}/breaches")).status_code == 401


# ---------------------------------------------------------------------------
# detection rules
# ---------------------------------------------------------------------------
async def test_a_restricted_crossing_at_a_closed_gate_is_recorded(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    result = await report(client, api_prefix, crossing(tripwire))
    assert result["recorded"] == 1
    assert result["sequences"] == [1]

    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    assert body["total"] == 1
    event = body["items"][0]
    assert event["review_status"] == "pending", "an event is a detection, not a finding"
    assert event["gate_code"] == "G3"
    assert event["has_clip"] is True
    assert "clip_uri" not in event, "the URI is a capability, not a display field"


async def test_a_crossing_in_the_permitted_direction_is_not_a_breach(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire
):
    """Somebody walking *out* through an entry-restricted line is leaving."""
    result = await report(client, api_prefix, crossing(tripwire, direction="out"))
    assert result["recorded"] == 0
    assert any("direction" in reason for reason in result["reasons"])


async def test_a_crossing_while_the_gate_is_open_is_not_a_breach(
    client: AsyncClient, api_prefix: str, session: AsyncSession, gate: Gate, tripwire: Tripwire
):
    """An open gate is a gate people are meant to walk through."""
    gate.is_open = True
    await session.commit()

    result = await report(client, api_prefix, crossing(tripwire))
    assert result["recorded"] == 0
    assert any("open" in reason for reason in result["reasons"])


async def test_a_scanned_pass_within_thirty_seconds_makes_it_authorised(
    client: AsyncClient, api_prefix: str, session: AsyncSession, gate: Gate, tripwire: Tripwire
):
    """Section 4/M5's cross-reference, and the reason most crossings never
    become events. A pilgrim whose pass was scanned as they walked through is a
    pilgrim, and recording them would bury the real events in noise."""
    moment = now_utc()
    slot = Slot(
        date=moment.date(),
        start_time=moment.time().replace(second=0, microsecond=0),
        end_time=(moment + timedelta(minutes=30)).time().replace(second=0, microsecond=0),
        gate_id=gate.id,
        capacity=100,
        walkin_reserve=25,
        booked_count=1,
    )
    session.add(slot)
    await session.flush()
    session.add(
        Pass(
            slot_id=slot.id,
            reference="WV-TEST01",
            holder_phone_hash="x" * 64,
            group_size=1,
            qr_secret="s" * 32,
            status="used",
            scanned_at=moment,
            scanned_gate_id=gate.id,
        )
    )
    await session.commit()

    result = await report(client, api_prefix, crossing(tripwire, occurred_at=moment.isoformat()))

    assert result["recorded"] == 0
    assert any("pass" in reason for reason in result["reasons"])


async def test_a_pass_scanned_outside_the_window_does_not_authorise(
    client: AsyncClient, api_prefix: str, session: AsyncSession, gate: Gate, tripwire: Tripwire
):
    """±30 seconds, not "some time that day". A pass scanned five minutes
    earlier does not cover somebody crossing now."""
    moment = now_utc()
    slot = Slot(
        date=moment.date(),
        start_time=moment.time().replace(second=0, microsecond=0),
        end_time=(moment + timedelta(minutes=30)).time().replace(second=0, microsecond=0),
        gate_id=gate.id,
        capacity=100,
        walkin_reserve=25,
        booked_count=1,
    )
    session.add(slot)
    await session.flush()
    session.add(
        Pass(
            slot_id=slot.id,
            reference="WV-TEST02",
            holder_phone_hash="y" * 64,
            group_size=1,
            qr_secret="t" * 32,
            status="used",
            scanned_at=moment - timedelta(minutes=5),
            scanned_gate_id=gate.id,
        )
    )
    await session.commit()

    result = await report(client, api_prefix, crossing(tripwire, occurred_at=moment.isoformat()))
    assert result["recorded"] == 1


async def test_the_pass_check_is_recorded_even_when_it_finds_nothing(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """"We checked and there was no pass" and "nobody checked" are different
    claims, and a reviewer six months later needs to know which this was."""
    await report(client, api_prefix, crossing(tripwire))

    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    assert body["items"][0]["pass_scan_checked"] is True


async def test_a_bad_clock_cannot_write_evidence_into_next_week(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire
):
    """`occurred_at` is inside the hash, so a bad timestamp is permanent."""
    future = (now_utc() + timedelta(days=7)).isoformat()
    result = await report(client, api_prefix, crossing(tripwire, occurred_at=future))

    assert result["recorded"] == 0
    assert any("timestamp" in reason for reason in result["reasons"])


async def test_one_unknown_tripwire_does_not_discard_the_batch(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire
):
    unknown = crossing(tripwire, tripwire_id=str(uuid.uuid4()))
    result = await report(client, api_prefix, unknown, crossing(tripwire))

    assert result["recorded"] == 1
    assert "TRIPWIRE_NOT_FOUND" in result["reasons"]


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------
async def test_records_link_to_their_predecessor(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    moment = now_utc()
    await report(
        client,
        api_prefix,
        crossing(tripwire, occurred_at=(moment - timedelta(seconds=60)).isoformat()),
        crossing(tripwire, occurred_at=(moment - timedelta(seconds=30)).isoformat()),
        crossing(tripwire, occurred_at=moment.isoformat()),
    )

    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    events = sorted(body["items"], key=lambda e: e["sequence"])

    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert events[0]["prev_hash"] == breach_service.GENESIS_HASH
    assert events[1]["prev_hash"] == events[0]["chain_hash"]
    assert events[2]["prev_hash"] == events[1]["chain_hash"]


async def test_a_fresh_ledger_verifies(client: AsyncClient, api_prefix: str, officer_token: str):
    body = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()
    assert body["intact"] is True
    assert body["events_checked"] == 0
    assert body["note_mr"]


async def test_a_populated_ledger_verifies(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))

    body = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()
    assert body["intact"] is True
    assert body["events_checked"] == 2
    assert body["breaks"] == []
    assert body["head_hash"]


async def test_editing_a_record_breaks_the_chain_visibly(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire, officer_token: str
):
    """The point of the whole module.

    The database trigger blocks an UPDATE through the application, so this test
    edits `payload_snapshot` the way someone with direct database access would.
    The chain must notice — that is what makes the ledger useful against exactly
    the pressure Section 4/M5 describes.
    """
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))

    # Rewrite the stored evidence out from under the hash.
    await session.execute(
        text(
            "UPDATE breach_events SET payload_snapshot = jsonb_set("
            "payload_snapshot, '{confidence}', '0.1') WHERE sequence = 1"
        )
    )
    await session.commit()

    body = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()

    assert body["intact"] is False
    problems = [b["problem"] for b in body["breaks"]]
    assert any("altered" in p for p in problems)
    assert body["breaks"][0]["sequence"] == 1


async def test_deleting_a_record_leaves_a_visible_gap(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire, officer_token: str
):
    """A row removed out of band shows up as both a sequence gap and a broken
    link — two independent signals, so covering one up does not hide the other."""
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire), crossing(tripwire))

    await session.execute(text("DELETE FROM breach_events WHERE sequence = 2"))
    await session.commit()

    body = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()

    assert body["intact"] is False
    problems = [b["problem"] for b in body["breaks"]]
    assert any("sequence gap" in p for p in problems)
    assert any("prev_hash" in p for p in problems)


async def test_the_database_itself_refuses_to_edit_evidence(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire
):
    """`trg_breach_evidence_immutable` from migration 0003.

    The application is not the only thing standing between a record and a
    motivated editor — an UPDATE that touches an evidence column raises even
    with a valid database session.
    """
    await report(client, api_prefix, crossing(tripwire))

    with pytest.raises(Exception) as caught:
        await session.execute(text("UPDATE breach_events SET occurred_at = now() WHERE sequence = 1"))
        await session.commit()
    assert "immutable" in str(caught.value).lower()
    await session.rollback()


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
async def test_an_event_is_not_a_finding_until_a_human_says_so(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.post(
        f"{api_prefix}/breaches/{breach_id}/review",
        json={"status": "verified"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 200
    assert response.json()["review_status"] == "verified"
    assert response.json()["reviewed_at"] is not None


async def test_authorising_a_record_needs_a_written_reason(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """"Authorised" with no reason is an assertion that somebody was allowed
    through and no record of who decided that."""
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    refused = await client.post(
        f"{api_prefix}/breaches/{breach_id}/review",
        json={"status": "authorised"},
        headers=bearer(officer_token),
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "REVIEW_REASON_REQUIRED"

    accepted = await client.post(
        f"{api_prefix}/breaches/{breach_id}/review",
        json={"status": "authorised", "reason": "Medical team escorted a patient through."},
        headers=bearer(officer_token),
    )
    assert accepted.status_code == 200


async def test_a_false_positive_also_needs_a_reason(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    refused = await client.post(
        f"{api_prefix}/breaches/{breach_id}/review",
        json={"status": "false_positive"},
        headers=bearer(officer_token),
    )
    assert refused.status_code == 400


async def test_a_review_cannot_be_set_back_to_pending(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """A review is a decision. Offering a way back to "undecided" would erase
    the fact that somebody looked."""
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.post(
        f"{api_prefix}/breaches/{breach_id}/review",
        json={"status": "pending"},
        headers=bearer(officer_token),
    )
    assert response.status_code == 422


async def test_reviewing_does_not_break_the_chain(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """Review columns are outside the hash on purpose. If they were not, the
    first officer to mark something verified would invalidate everything after
    it — and a chain that fires constantly means nothing."""
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()

    for item in body["items"]:
        await client.post(
            f"{api_prefix}/breaches/{item['id']}/review",
            json={"status": "verified"},
            headers=bearer(officer_token),
        )

    verify = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()
    assert verify["intact"] is True


# ---------------------------------------------------------------------------
# clip access
# ---------------------------------------------------------------------------
async def test_viewing_a_clip_needs_the_password_again(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """Section 4/M5: "clip playback requires re-authentication"."""
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    wrong = await client.post(
        f"{api_prefix}/breaches/{breach_id}/clip",
        json={"password": "not-the-password", "purpose": "Reviewing gate 3 backlog"},
        headers=bearer(officer_token),
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "REAUTH_REQUIRED"

    right = await client.post(
        f"{api_prefix}/breaches/{breach_id}/clip",
        json={"password": PASSWORD, "purpose": "Reviewing gate 3 backlog"},
        headers=bearer(officer_token),
    )
    assert right.status_code == 200
    assert right.json()["clip_uri"].startswith("s3://")
    assert right.json()["notice_mr"]


async def test_a_clip_view_needs_a_stated_purpose(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """A viewing with no stated reason is the one an inquiry asks about."""
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.post(
        f"{api_prefix}/breaches/{breach_id}/clip",
        json={"password": PASSWORD, "purpose": ""},
        headers=bearer(officer_token),
    )
    assert response.status_code == 422


async def test_every_clip_view_is_logged_on_the_record(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """The trail sits on the record so a reviewer deciding whether to watch a
    clip can see who already has."""
    await report(client, api_prefix, crossing(tripwire))
    listing = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = listing["items"][0]["id"]

    await client.post(
        f"{api_prefix}/breaches/{breach_id}/clip",
        json={"password": PASSWORD, "purpose": "Governance review for 12 July"},
        headers=bearer(officer_token),
    )

    detail = (await client.get(f"{api_prefix}/breaches/{breach_id}", headers=bearer(officer_token))).json()
    assert len(detail["clip_views"]) == 1
    assert detail["clip_views"][0]["purpose"] == "Governance review for 12 July"


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
async def test_an_administrator_cannot_remove_evidence(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str, admin_token: str
):
    """The one permission Administrator is deliberately denied.

    The temple administrator who might come under pressure to make a record go
    away is not the person who can.
    """
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.request(
        "DELETE",
        f"{api_prefix}/breaches/{breach_id}",
        json={"reason": "Requested by the trust office over the telephone."},
        headers=bearer(admin_token),
    )
    assert response.status_code == 403


async def test_a_system_admin_redaction_keeps_the_record_and_the_chain(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str, sysadmin_token: str
):
    """Deletion redacts the clip, never the record.

    A real row deletion would break the chain, and a broken chain cannot
    distinguish an authorised removal from tampering — the one distinction the
    ledger exists to make.
    """
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.request(
        "DELETE",
        f"{api_prefix}/breaches/{breach_id}",
        json={"reason": "Clip captured a bystander's medical treatment; removed on request."},
        headers=bearer(sysadmin_token),
    )
    assert response.status_code == 200
    redacted = response.json()

    assert redacted["has_clip"] is False
    assert redacted["redacted_at"] is not None
    assert redacted["redaction_reason"]
    # The hash of what was removed survives the removal.
    assert redacted["clip_sha256"] == "a" * 64
    assert redacted["chain_hash"]

    verify = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()
    assert verify["intact"] is True, "redaction must not break the chain"
    assert verify["events_checked"] == 2, "the record itself is still there"


async def test_redaction_needs_a_written_reason(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str, sysadmin_token: str
):
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    response = await client.request(
        "DELETE",
        f"{api_prefix}/breaches/{breach_id}",
        json={"reason": "no"},
        headers=bearer(sysadmin_token),
    )
    assert response.status_code == 422


async def test_a_redaction_is_recorded_in_the_append_only_audit_log(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire,
    officer_token: str, sysadmin_token: str
):
    """"The deletion itself is logged permanently." The audit log has a database
    trigger refusing UPDATE and DELETE, so the record of the removal outlives
    anyone's ability to remove it."""
    await report(client, api_prefix, crossing(tripwire))
    body = (await client.get(f"{api_prefix}/breaches", headers=bearer(officer_token))).json()
    breach_id = body["items"][0]["id"]

    await client.request(
        "DELETE",
        f"{api_prefix}/breaches/{breach_id}",
        json={"reason": "Duplicate record created during a camera restart."},
        headers=bearer(sysadmin_token),
    )

    row = (
        await session.execute(
            text("SELECT action, meta FROM audit_log WHERE action = 'breach.deleted'")
        )
    ).first()
    assert row is not None
    assert "reason" in row[1]
    assert row[1]["chain_hash"]


# ---------------------------------------------------------------------------
# governance report
# ---------------------------------------------------------------------------
async def test_the_daily_summary_carries_no_personal_data(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """The artefact the trust takes to a governance meeting."""
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))

    body = (await client.get(f"{api_prefix}/breaches/summary", headers=bearer(officer_token))).json()

    assert body["total"] == 2
    assert body["by_review_status"]["pending"] == 2
    assert body["by_gate_hour"][0]["gate_code"] == "G3"
    assert body["chain_intact"] is True
    assert body["notice_mr"]

    serialised = str(body)
    for banned in ("phone", "name_mr", "holder", "track_id"):
        assert banned not in serialised


async def test_the_summary_says_whether_its_own_ledger_verifies(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire, officer_token: str
):
    """A breach report that does not say this invites the question at the worst
    possible moment."""
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))
    await session.execute(text("DELETE FROM breach_events WHERE sequence = 1"))
    await session.commit()

    body = (await client.get(f"{api_prefix}/breaches/summary", headers=bearer(officer_token))).json()
    assert body["chain_intact"] is False


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
async def test_retention_clears_the_clip_and_keeps_the_record(
    client: AsyncClient, api_prefix: str, session: AsyncSession, tripwire: Tripwire, officer_token: str
):
    """A ledger whose rows disappeared on a 90-day timer would fail its own
    verification every quarter by design."""
    await report(client, api_prefix, crossing(tripwire))

    retention = await config_service.get_int(session, "breach_retention_days")
    purged = await breach_service.purge_expired_clips(
        session, at=now_utc() + timedelta(days=retention + 1)
    )
    await session.commit()

    assert purged == [1]

    event = await session.scalar(select_one_event())
    assert event is not None
    assert event.clip_uri is None
    assert event.clip_sha256 == "a" * 64, "the hash of what was held survives the purge"

    verify = (await client.get(f"{api_prefix}/breaches/verify", headers=bearer(officer_token))).json()
    assert verify["intact"] is True


def select_one_event():
    from sqlalchemy import select

    return select(BreachEvent).where(BreachEvent.sequence == 1)


# ---------------------------------------------------------------------------
# tripwires
# ---------------------------------------------------------------------------
async def test_a_tripwire_reports_how_many_events_it_has_produced(
    client: AsyncClient, api_prefix: str, tripwire: Tripwire, officer_token: str
):
    """A line generating hundreds of pending events is drawn wrong, and without
    this column that shows up as a reviewer's backlog rather than a config
    error."""
    await report(client, api_prefix, crossing(tripwire), crossing(tripwire))

    body = (await client.get(f"{api_prefix}/tripwires", headers=bearer(officer_token))).json()
    assert body[0]["event_count"] == 2
    assert body[0]["pending_count"] == 2
    assert body[0]["points"] == [[0.2, 0.5], [0.8, 0.5]]


async def test_tripwire_points_must_be_normalised(
    client: AsyncClient, api_prefix: str, camera: Camera, admin_token: str
):
    """Normalised coordinates survive a change of stream resolution. A line
    drawn in pixels against a 1080p feed points somewhere else the day the
    camera is replaced with a 4K one."""
    response = await client.post(
        f"{api_prefix}/tripwires",
        json={
            "camera_id": str(camera.id),
            "name": "bad line",
            "points": [[640, 360], [1280, 360]],
            "restricted_direction": "in",
        },
        headers=bearer(admin_token),
    )
    assert response.status_code == 400
    assert "normalised" in response.json()["error"]["details"]["reason"]


async def test_a_security_officer_cannot_move_the_line(
    client: AsyncClient, api_prefix: str, camera: Camera, officer_token: str
):
    """Reviewing an event and configuring what counts as one are different
    powers. The reviewer should not also be able to quietly move the line."""
    response = await client.post(
        f"{api_prefix}/tripwires",
        json={
            "camera_id": str(camera.id),
            "name": "new line",
            "points": [[0.1, 0.5], [0.9, 0.5]],
            "restricted_direction": "in",
        },
        headers=bearer(officer_token),
    )
    assert response.status_code == 403
