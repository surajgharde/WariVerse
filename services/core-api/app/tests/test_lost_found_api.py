"""End-to-end lost-and-found property (Track 1, item 2).

The behaviours pinned here are the ones that are rules rather than features —
the ones that quietly stop being true when somebody refactors the scorer:

* the public register never leaks the identifying mark or the photo;
* nothing is ever matched automatically, at any score;
* a found item cannot be handed over without a claimant name and a note;
* a wrong identifying mark is refused without telling the guesser how close;
* a handover closes the owner's own lost report too;
* a walking aid is matched on a shorter clock than a bag.

Needs Postgres and Redis (`docker compose up -d db redis`).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, now_utc
from app.models import AuditLog, Facility, LostFoundItem, LostFoundMatch, Zone
from app.models.lostfound import LostFoundStatus
from app.services import lostfound_service
from app.services.audit_service import AuditAction

pytestmark = [pytest.mark.db, pytest.mark.redis]

MARK = "आत तुळशीची माळ आणि स्टीलचा डबा"


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
async def other_zone(session: AsyncSession) -> Zone:
    record = Zone(
        code="GH",
        name="Ghat",
        name_mr="घाट",
        geom="SRID=4326;POLYGON((75.340 17.690, 75.343 17.690, 75.343 17.693, 75.340 17.693, 75.340 17.690))",
        area_m2=900.0,
        capacity_persons=1800,
        zone_type="ghat",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def desk(session: AsyncSession, zone: Zone) -> Facility:
    record = Facility(
        zone_id=zone.id,
        type="lost_and_found",
        name="Lost and Found Desk 1",
        name_mr="हरवले-सापडले कक्ष १",
        location="SRID=4326;POINT(75.3305 17.6795)",
        status="operational",
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
async def other_pilgrim_token(make_user):
    user = await make_user(phone="9811111111", role=Role.PILGRIM, password=None, name="दुसरा")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


@pytest.fixture
async def volunteer_token(make_user):
    user = await make_user(phone="9000000002", role=Role.VOLUNTEER, name="स्वयंसेवक")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


async def report_lost(
    client: AsyncClient,
    api_prefix: str,
    token: str,
    *,
    category: str = "bag",
    description: str = "निळी कापडी पिशवी",
    colour: str | None = "blue",
    marks: str | None = MARK,
    zone_id: uuid.UUID | None = None,
    occurred_at=None,
) -> dict:
    body: dict = {"category": category, "description": description, "colour": colour}
    if marks:
        body["distinguishing_marks"] = marks
    if zone_id:
        body["zone_id"] = str(zone_id)
    if occurred_at:
        body["occurred_at"] = occurred_at.isoformat()
    response = await client.post(f"{api_prefix}/lost-found/lost", json=body, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


async def register_found(
    client: AsyncClient,
    api_prefix: str,
    token: str,
    *,
    category: str = "bag",
    description: str = "निळी पिशवी",
    colour: str | None = "blue",
    marks: str | None = MARK,
    zone_id: uuid.UUID | None = None,
    desk_id: uuid.UUID | None = None,
    occurred_at=None,
) -> dict:
    body: dict = {"category": category, "description": description, "colour": colour}
    if marks:
        body["distinguishing_marks"] = marks
    if zone_id:
        body["zone_id"] = str(zone_id)
    if desk_id:
        body["custody_facility_id"] = str(desk_id)
    if occurred_at:
        body["occurred_at"] = occurred_at.isoformat()
    response = await client.post(f"{api_prefix}/lost-found/found", json=body, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
async def test_a_pilgrim_can_report_a_loss_and_gets_a_reference(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, zone: Zone
) -> None:
    body = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    assert body["kind"] == "lost"
    assert body["status"] == "open"
    assert body["reference"].startswith("LF-")
    # No I/O/0/1 — these get read aloud across a help desk in a crowd.
    assert not (set(body["reference"][3:]) & set("IO01"))
    assert body["zone_code"] == "TC"


async def test_a_pilgrim_cannot_register_a_found_item(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """Anyone may say "I lost a bag". Only a named volunteer may say "here is one"."""
    response = await client.post(
        f"{api_prefix}/lost-found/found",
        json={"category": "bag", "description": "निळी पिशवी"},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 403


async def test_a_found_item_filed_against_a_missing_desk_is_refused(
    client: AsyncClient, api_prefix: str, volunteer_token: str
) -> None:
    """An item nobody can be sent to collect is worse than no record."""
    response = await client.post(
        f"{api_prefix}/lost-found/found",
        json={
            "category": "bag",
            "description": "निळी पिशवी",
            "custody_facility_id": str(uuid.uuid4()),
        },
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the privacy rule
# ---------------------------------------------------------------------------
async def test_the_public_register_never_carries_the_identifying_mark(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone, desk: Facility
) -> None:
    """The mark is the password. Publishing it is publishing the answer."""
    await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, desk_id=desk.id)

    response = await client.get(f"{api_prefix}/lost-found/search", headers=bearer(pilgrim_token))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1

    entry = items[0]
    assert entry["category"] == "bag"
    assert entry["custody_desk_mr"] == "हरवले-सापडले कक्ष १"
    # Enough to walk to the right desk, and nothing more.
    for leaked in ("distinguishing_marks", "photo_uri", "id", "reported_at", "occurred_at"):
        assert leaked not in entry, f"public search leaked {leaked}"
    # A date, not a timestamp — the hour it was handed in is the owner's to know.
    assert len(entry["found_on"]) == 10


async def test_a_stranger_cannot_read_someone_elses_record(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, other_pilgrim_token: str, zone: Zone
) -> None:
    mine = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    response = await client.get(
        f"{api_prefix}/lost-found/{mine['id']}", headers=bearer(other_pilgrim_token)
    )
    assert response.status_code == 403


async def test_a_pilgrim_reads_their_own_record_in_full(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, zone: Zone
) -> None:
    mine = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    response = await client.get(f"{api_prefix}/lost-found/{mine['id']}", headers=bearer(pilgrim_token))
    assert response.status_code == 200, response.text
    # Their own words, on their own record.
    assert response.json()["distinguishing_marks"] == MARK


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
async def test_a_matching_found_item_is_suggested_but_never_auto_matched(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone, desk: Facility
) -> None:
    """The core rule of the module: it ranks, a human decides."""
    await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, desk_id=desk.id)
    lost = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    assert lost["status"] == "open", "a suggestion is not a match"
    assert lost["matched_item_id"] is None
    assert len(lost["suggestions"]) == 1

    suggestion = lost["suggestions"][0]
    assert suggestion["decision"] == "pending"
    assert suggestion["is_strong"] is True
    # The desk reads the reasons, not the number.
    assert suggestion["reasons"]["same_zone"] is True
    assert suggestion["reasons"]["category"] == "bag"
    # The counterpart comes back at public detail even for the owner's own view.
    assert "distinguishing_marks" not in suggestion["counterpart"]


async def test_a_different_category_is_never_suggested(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone
) -> None:
    """Not a low score — a different object. Letting it through buries the real one."""
    await register_found(
        client, api_prefix, volunteer_token, category="phone", description="काळा फोन", zone_id=zone.id
    )
    lost = await report_lost(client, api_prefix, pilgrim_token, category="bag", zone_id=zone.id)
    assert lost["suggestions"] == []


async def test_an_item_found_before_it_was_lost_is_never_suggested(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone
) -> None:
    """The most common way a lost-property system produces confident nonsense."""
    await register_found(
        client,
        api_prefix,
        volunteer_token,
        zone_id=zone.id,
        occurred_at=now_utc() - timedelta(hours=6),
    )
    lost = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id, occurred_at=now_utc())
    assert lost["suggestions"] == []


async def test_accepting_a_match_links_both_rows_and_needs_a_volunteer(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    volunteer_token: str,
    pilgrim_token: str,
    zone: Zone,
    desk: Facility,
) -> None:
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, desk_id=desk.id)
    lost = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    # A pilgrim cannot match their own report to an item they have not seen.
    refused = await client.post(
        f"{api_prefix}/lost-found/{lost['id']}/match",
        json={"found_item_id": found["id"], "accept": True},
        headers=bearer(pilgrim_token),
    )
    assert refused.status_code == 403

    response = await client.post(
        f"{api_prefix}/lost-found/{lost['id']}/match",
        json={"found_item_id": found["id"], "accept": True},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "matched"
    assert response.json()["matched_item_id"] == found["id"]

    # Symmetric: either side of the desk can be looked up first.
    other = await session.get(LostFoundItem, uuid.UUID(found["id"]))
    await session.refresh(other)
    assert str(other.matched_item_id) == lost["id"]
    assert other.status == LostFoundStatus.MATCHED


async def test_a_rejected_suggestion_is_kept_not_deleted(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    volunteer_token: str,
    pilgrim_token: str,
    zone: Zone,
) -> None:
    """The only evidence the scorer is wrong in a particular way."""
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id)
    lost = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    response = await client.post(
        f"{api_prefix}/lost-found/{lost['id']}/match",
        json={"found_item_id": found["id"], "accept": False, "note": "वेगळी पिशवी"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "open", "a rejection leaves both records open"

    match = await session.scalar(
        select(LostFoundMatch).where(LostFoundMatch.lost_item_id == uuid.UUID(lost["id"]))
    )
    assert match is not None
    await session.refresh(match)
    assert match.decision == "rejected"
    assert match.decided_by is not None


async def test_two_records_of_the_same_kind_cannot_be_paired(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone
) -> None:
    first = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)
    second = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)

    response = await client.post(
        f"{api_prefix}/lost-found/{first['id']}/match",
        json={"lost_item_id": second["id"], "accept": True},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "LOSTFOUND_KIND_MISMATCH"


# ---------------------------------------------------------------------------
# claiming and handover
# ---------------------------------------------------------------------------
async def test_a_claim_with_the_right_mark_passes_without_returning_the_mark(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone, desk: Facility
) -> None:
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, desk_id=desk.id)

    response = await client.post(
        f"{api_prefix}/lost-found/{found['id']}/claim",
        # Half the remembered detail is a pass — plainly the same bag.
        json={"identifying_mark": "तुळशीची माळ", "claimant_name": "सखुबाई"},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "claimed", "a verified claim does not empty the shelf"
    assert body["claimed_by_name"] == "सखुबाई"
    assert body["handed_over_at"] is None


async def test_a_wrong_mark_is_refused_without_saying_how_close(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone
) -> None:
    """A precise failure turns verification into an oracle to iterate against."""
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id)

    response = await client.post(
        f"{api_prefix}/lost-found/{found['id']}/claim",
        json={"identifying_mark": "लाल छत्री", "claimant_name": "कोणीतरी"},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "LOSTFOUND_CLAIM_UNVERIFIED"
    assert error["message_mr"]
    # Nothing about overlap, distance, or the stored text.
    assert "overlap" not in error["details"]


async def test_an_item_with_no_recorded_mark_cannot_be_claimed_remotely(
    client: AsyncClient, api_prefix: str, volunteer_token: str, pilgrim_token: str, zone: Zone
) -> None:
    """It says so, rather than passing by default."""
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, marks=None)

    response = await client.post(
        f"{api_prefix}/lost-found/{found['id']}/claim",
        json={"identifying_mark": "काहीही", "claimant_name": "कोणीतरी"},
        headers=bearer(pilgrim_token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LOSTFOUND_NOT_VERIFIABLE"


async def test_handover_needs_a_note_and_closes_the_owners_lost_report(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    volunteer_token: str,
    pilgrim_token: str,
    zone: Zone,
    desk: Facility,
) -> None:
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id, desk_id=desk.id)
    lost = await report_lost(client, api_prefix, pilgrim_token, zone_id=zone.id)
    await client.post(
        f"{api_prefix}/lost-found/{lost['id']}/match",
        json={"found_item_id": found["id"], "accept": True},
        headers=bearer(volunteer_token),
    )

    # A note is not optional: an item that left with no sentence against it is
    # indistinguishable afterwards from one stolen off the shelf.
    missing_note = await client.post(
        f"{api_prefix}/lost-found/{found['id']}/handover",
        json={"claimant_name": "सखुबाई"},
        headers=bearer(volunteer_token),
    )
    assert missing_note.status_code == 422

    response = await client.post(
        f"{api_prefix}/lost-found/{found['id']}/handover",
        json={"claimant_name": "सखुबाई", "note": "फोटो आणि खूण जुळली"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "returned"
    assert body["handed_over_at"] is not None
    # Retention starts at closure, never at report.
    assert body["purge_after"] > body["resolved_at"]

    owner_side = await session.get(LostFoundItem, uuid.UUID(lost["id"]))
    await session.refresh(owner_side)
    assert owner_side.status == LostFoundStatus.RETURNED, "the owner's own record must not stay open"


async def test_handover_is_audited_with_who_gave_it_to_whom(
    client: AsyncClient, api_prefix: str, session: AsyncSession, volunteer_token: str, zone: Zone
) -> None:
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id)
    await client.post(
        f"{api_prefix}/lost-found/{found['id']}/handover",
        json={"claimant_name": "सखुबाई", "note": "खूण जुळली"},
        headers=bearer(volunteer_token),
    )

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.LOSTFOUND_HANDED_OVER)
    )
    assert entry is not None
    assert entry.actor_id is not None, "an item left the desk — somebody handed it over"
    assert entry.meta["claimant"] == "सखुबाई"


async def test_returned_cannot_be_set_through_the_generic_update(
    client: AsyncClient, api_prefix: str, volunteer_token: str, zone: Zone
) -> None:
    """That transition needs a claimant and a note, so it lives on /handover."""
    found = await register_found(client, api_prefix, volunteer_token, zone_id=zone.id)

    response = await client.patch(
        f"{api_prefix}/lost-found/{found['id']}",
        json={"status": "returned"},
        headers=bearer(volunteer_token),
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# the scorer itself
# ---------------------------------------------------------------------------
def _item(**kwargs) -> LostFoundItem:
    base = {
        "reference": "LF-TEST01",
        "kind": "lost",
        "category": "bag",
        "description": "निळी कापडी पिशवी",
        "occurred_at": now_utc(),
        "reported_at": now_utc(),
        "status": "open",
        "language": "mr",
    }
    base.update(kwargs)
    return LostFoundItem(**base)


def test_the_scorer_rewards_the_same_zone_and_penalises_a_different_one() -> None:
    zone_a, zone_b = uuid.uuid4(), uuid.uuid4()
    at = now_utc()

    lost = _item(kind="lost", zone_id=zone_a, occurred_at=at)
    same = lostfound_service.score_pair(lost, _item(kind="found", zone_id=zone_a, occurred_at=at))
    different = lostfound_service.score_pair(lost, _item(kind="found", zone_id=zone_b, occurred_at=at))

    assert same is not None and different is not None
    assert same.score > different.score
    assert same.reasons["same_zone"] is True
    assert different.reasons["same_zone"] is False


def test_a_walking_aid_is_matched_on_a_much_shorter_clock() -> None:
    """A lost walking stick is a mobility emergency, not lost property.

    An owner who has been without it for a day has already had to solve the
    problem another way, so an old suggestion is worse than none.
    """
    assert lostfound_service.window_hours("walking_aid") < lostfound_service.window_hours("bag")

    at = now_utc()
    lost = _item(kind="lost", category="walking_aid", description="काठी", occurred_at=at)
    late = _item(
        kind="found", category="walking_aid", description="काठी", occurred_at=at + timedelta(hours=20)
    )
    assert lostfound_service.score_pair(lost, late) is None

    bag_lost = _item(kind="lost", occurred_at=at)
    bag_late = _item(kind="found", occurred_at=at + timedelta(hours=20))
    assert lostfound_service.score_pair(bag_lost, bag_late) is not None


def test_claim_verification_never_returns_the_stored_mark() -> None:
    found = _item(kind="found", distinguishing_marks=MARK)

    passed, overlap = lostfound_service.verify_claim(found, "तुळशीची माळ")
    assert passed is True
    assert isinstance(overlap, float)

    failed, _ = lostfound_service.verify_claim(found, "लाल छत्री")
    assert failed is False

    # No mark on file is not a pass.
    blank, _ = lostfound_service.verify_claim(_item(kind="found"), "काहीही")
    assert blank is False
