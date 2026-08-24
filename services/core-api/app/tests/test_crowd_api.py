"""End-to-end Phase 3: ingest -> threshold -> alert -> read back.

Section 4/M2's acceptance criterion is "trigger a CRITICAL alert when the
threshold is crossed".  `test_a_critical_reading_raises_a_critical_alert` is
that criterion, and the rest of this file is the behaviour around it that makes
the alert trustworthy: staleness, confidence, deduplication, auto-resolution,
and the line between what an operator sees and what a pilgrim sees.

Needs Postgres and Redis (`docker compose up -d db redis`).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, now_utc
from app.models import Camera, Zone
from app.services import alert_service, config_service, crowd_service

pytestmark = [pytest.mark.db, pytest.mark.redis]

AI_HEADERS = {"x-ai-service-token": "test-ai-service-token"}

#: Temple Core from the seed: 1200 m².  These counts are chosen so the density
#: lands unambiguously inside a band.
AREA = 1200.0
SAFE_COUNT = 1_200      # 1.0 p/m²
HIGH_COUNT = 5_000      # 4.17 p/m²
CRITICAL_COUNT = 7_000  # 5.83 p/m²


@pytest.fixture(autouse=True)
def _clean_caches():
    config_service.clear_cache()
    alert_service.reset_streaks()
    yield
    config_service.clear_cache()
    alert_service.reset_streaks()


@pytest.fixture
async def zone(session: AsyncSession) -> Zone:
    record = Zone(
        code="TC",
        name="Temple Core",
        name_mr="मंदिर गाभारा",
        geom="SRID=4326;POLYGON((75.33 17.679, 75.331 17.679, 75.331 17.68, 75.33 17.68, 75.33 17.679))",
        area_m2=AREA,
        capacity_persons=2400,
        zone_type="temple_core",
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def camera(session: AsyncSession, zone: Zone) -> Camera:
    record = Camera(zone_id=zone.id, name="CAM-TC-01", status="online", last_heartbeat_at=now_utc())
    session.add(record)
    await session.commit()
    return record


@pytest.fixture
async def officer_token(make_user):
    user = await make_user(phone="9000000003", role=Role.SECURITY_OFFICER, name="सुरक्षा अधिकारी")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=False)
    return token


@pytest.fixture
async def pilgrim_token(make_user):
    user = await make_user(phone="9876543210", role=Role.PILGRIM, password=None, name="यात्रेकरू")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=False)
    return token


def reading(zone: Zone, count: int, **overrides) -> dict:
    payload = {
        "zone_id": str(zone.id),
        "person_count": count,
        "observed_at": now_utc().isoformat(),
        "camera_count": 1,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


async def ingest(client: AsyncClient, api_prefix: str, *readings: dict, source: str = "sim") -> dict:
    response = await client.post(
        f"{api_prefix}/ingest/density",
        json={"source": source, "readings": list(readings)},
        headers=AI_HEADERS,
    )
    assert response.status_code == 202, response.text
    return response.json()


# ---------------------------------------------------------------------------
# the boundary
# ---------------------------------------------------------------------------
async def test_ingest_requires_the_service_token(client: AsyncClient, api_prefix: str, zone: Zone):
    response = await client.post(
        f"{api_prefix}/ingest/density", json={"source": "sim", "readings": [reading(zone, 100)]}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_a_user_token_cannot_publish_readings(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The ingest path is for machines. A signed-in officer is not one, and a
    stolen operator session must not be able to fabricate crowd data."""
    response = await client.post(
        f"{api_prefix}/ingest/density",
        json={"source": "sim", "readings": [reading(zone, 100)]},
        headers={"Authorization": f"Bearer {officer_token}"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
async def test_a_reading_lands_and_reads_back(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    response = await client.get(f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {officer_token}"})
    assert response.status_code == 200
    body = response.json()

    assert body["zones"][0]["zone_code"] == "TC"
    assert body["zones"][0]["person_count"] == SAFE_COUNT
    assert body["zones"][0]["density"] == pytest.approx(SAFE_COUNT / AREA, rel=1e-3)
    assert body["zones"][0]["level"] == "safe"
    assert body["unknown_zones"] == []


async def test_density_is_recomputed_from_the_surveyed_area(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The database owns the ground truth for area.  An engine running against
    a zone we have since re-surveyed has stale arithmetic; ours does not."""
    await ingest(client, api_prefix, reading(zone, 2400, density=99.0))

    response = await client.get(f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {officer_token}"})
    zone_out = response.json()["zones"][0]

    assert zone_out["density"] == pytest.approx(2.0, rel=1e-3)
    assert any("surveyed" in note for note in zone_out["notes"])


async def test_a_reading_with_no_camera_is_marked_as_an_estimate(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """Section 0 rule 3.  A number nobody measured must not look like one
    somebody did."""
    await ingest(client, api_prefix, reading(zone, 3000, camera_count=0), source="live")

    response = await client.get(f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {officer_token}"})
    zone_out = response.json()["zones"][0]

    assert zone_out["confidence"] <= 0.4
    assert any("estimate" in note for note in zone_out["notes"])


async def test_one_bad_reading_does_not_discard_the_batch(
    client: AsyncClient, api_prefix: str, zone: Zone
):
    """Dropping forty zones over one malformed row is the wrong trade
    during a surge."""
    body = await ingest(
        client,
        api_prefix,
        reading(zone, SAFE_COUNT),
        {"zone_code": "NOPE", "person_count": 10, "observed_at": now_utc().isoformat()},
    )
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    assert "unknown" in body["rejections"][0]["reason"]


async def test_a_reading_from_the_future_is_refused(client: AsyncClient, api_prefix: str, zone: Zone):
    """A container with a wrong clock must not write into next Tuesday."""
    future = (now_utc() + timedelta(hours=2)).isoformat()
    body = await ingest(client, api_prefix, reading(zone, 100, observed_at=future))

    assert body["accepted"] == 0
    assert "clock" in body["rejections"][0]["reason"]


async def test_a_very_old_reading_is_refused(client: AsyncClient, api_prefix: str, zone: Zone):
    stale = (now_utc() - timedelta(hours=3)).isoformat()
    body = await ingest(client, api_prefix, reading(zone, 100, observed_at=stale))
    assert body["accepted"] == 0


async def test_a_zone_can_be_addressed_by_its_code(client: AsyncClient, api_prefix: str, zone: Zone):
    body = await ingest(
        client, api_prefix, {"zone_code": "tc", "person_count": 500, "observed_at": now_utc().isoformat()}
    )
    assert body["accepted"] == 1


# ---------------------------------------------------------------------------
# alerts — the acceptance criterion
# ---------------------------------------------------------------------------
async def test_a_critical_reading_raises_a_critical_alert(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """Section 4/M2 acceptance: trigger a CRITICAL alert when the threshold is
    crossed."""
    body = await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    assert body["alerts_raised"] == 1

    response = await client.get(f"{api_prefix}/alerts", headers={"Authorization": f"Bearer {officer_token}"})
    alerts = response.json()["items"]

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["severity"] == "critical"
    assert alert["zone_code"] == "TC"
    assert alert["rule_id"] == "R-M2-01"
    assert alert["trigger_value"] > 5.0
    # The action, in both languages, on the alert itself.
    assert "stop intake" in alert["recommended_action"].lower()
    assert alert["recommended_action_mr"]


async def test_a_safe_reading_raises_nothing(client: AsyncClient, api_prefix: str, zone: Zone):
    body = await ingest(client, api_prefix, reading(zone, SAFE_COUNT))
    assert body["alerts_raised"] == 0


async def test_a_persisting_condition_does_not_flood_the_feed(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """A zone sitting at 5.8 p/m² for two minutes is one situation, not twelve
    alerts.  Wallpaper is how the next real alert gets missed."""
    for _ in range(12):
        await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))

    response = await client.get(f"{api_prefix}/alerts", headers={"Authorization": f"Bearer {officer_token}"})
    assert response.json()["total"] == 1


async def test_an_open_alert_keeps_the_worst_value_seen(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """An operator arriving late needs the peak, not the last ten seconds."""
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    await ingest(client, api_prefix, reading(zone, 9_000, stagnation_index=0.95))
    await ingest(client, api_prefix, reading(zone, 6_200, stagnation_index=0.9))

    response = await client.get(f"{api_prefix}/alerts", headers={"Authorization": f"Bearer {officer_token}"})
    assert response.json()["items"][0]["trigger_value"] == pytest.approx(9_000 / AREA, rel=1e-3)


async def test_an_alert_closes_itself_once_the_zone_stays_calm(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))

    headers = {"Authorization": f"Bearer {officer_token}"}
    live = await client.get(f"{api_prefix}/alerts", headers=headers)
    assert live.json()["total"] == 1

    # One quiet window is noise; three across thirty seconds is a trend.
    for _ in range(alert_service.CALM_READINGS_TO_RESOLVE):
        await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    still_live = await client.get(f"{api_prefix}/alerts", headers=headers)
    assert still_live.json()["total"] == 0

    everything = await client.get(f"{api_prefix}/alerts?live_only=false", headers=headers)
    assert everything.json()["items"][0]["status"] == "resolved"


async def test_one_calm_window_does_not_close_an_alert(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    response = await client.get(f"{api_prefix}/alerts", headers={"Authorization": f"Bearer {officer_token}"})
    assert response.json()["total"] == 1


async def test_a_stalled_but_merely_high_zone_alerts(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The reading raw density would call safe enough to ignore."""
    body = await ingest(client, api_prefix, reading(zone, 4_300, stagnation_index=0.85))
    assert body["alerts_raised"] == 1

    response = await client.get(f"{api_prefix}/alerts", headers={"Authorization": f"Bearer {officer_token}"})
    alert = response.json()["items"][0]
    assert alert["type"] == "stagnation"
    assert alert["severity"] == "critical"


# ---------------------------------------------------------------------------
# operator actions
# ---------------------------------------------------------------------------
async def test_acknowledging_is_audited_with_how_long_it_took(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str, session: AsyncSession
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    headers = {"Authorization": f"Bearer {officer_token}"}

    listed = await client.get(f"{api_prefix}/alerts", headers=headers)
    alert_id = listed.json()["items"][0]["id"]

    response = await client.post(
        f"{api_prefix}/alerts/{alert_id}/ack", json={"note": "gate G2 held"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"

    from sqlalchemy import select

    from app.models import AuditLog

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == "alert.acknowledged", AuditLog.target_id == alert_id)
    )
    assert entry is not None
    assert entry.meta["rule_id"] == "R-M2-01"
    assert entry.meta["seconds_to_acknowledge"] >= 0


async def test_a_volunteer_can_see_alerts_but_not_acknowledge_them(
    client: AsyncClient, api_prefix: str, zone: Zone, make_user
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))

    user = await make_user(phone="9000000004", role=Role.VOLUNTEER)
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    headers = {"Authorization": f"Bearer {token}"}

    listed = await client.get(f"{api_prefix}/alerts", headers=headers)
    assert listed.status_code == 200
    alert_id = listed.json()["items"][0]["id"]

    denied = await client.post(f"{api_prefix}/alerts/{alert_id}/ack", json={}, headers=headers)
    assert denied.status_code == 403


async def test_resolving_records_what_was_done(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str, session: AsyncSession
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    headers = {"Authorization": f"Bearer {officer_token}"}
    alert_id = (await client.get(f"{api_prefix}/alerts", headers=headers)).json()["items"][0]["id"]

    response = await client.post(
        f"{api_prefix}/alerts/{alert_id}/resolve",
        json={"resolution": "Held intake at G2 for 6 minutes, opened the east exit."},
        headers=headers,
    )
    assert response.status_code == 200

    from sqlalchemy import select

    from app.models import AuditLog

    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "alert.resolved"))
    assert entry is not None
    assert "east exit" in entry.meta["resolution"]


async def test_a_closed_alert_cannot_be_closed_twice(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT, stagnation_index=0.9))
    headers = {"Authorization": f"Bearer {officer_token}"}
    alert_id = (await client.get(f"{api_prefix}/alerts", headers=headers)).json()["items"][0]["id"]

    await client.post(f"{api_prefix}/alerts/{alert_id}/resolve", json={"resolution": "handled"}, headers=headers)
    again = await client.post(
        f"{api_prefix}/alerts/{alert_id}/resolve", json={"resolution": "handled"}, headers=headers
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ALERT_ALREADY_CLOSED"


async def test_the_rule_table_is_published_to_operators(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    """"Why is it telling me to close the gate" must have an answer."""
    response = await client.get(
        f"{api_prefix}/alerts/rules", headers={"Authorization": f"Bearer {officer_token}"}
    )
    assert response.status_code == 200
    rules = response.json()

    assert len(rules) >= 5
    assert {r["id"] for r in rules} >= {"R-M2-01", "R-M2-06"}
    assert all(r["action_mr"] for r in rules)


# ---------------------------------------------------------------------------
# public vs operator view
# ---------------------------------------------------------------------------
async def test_the_public_view_carries_no_head_count(client: AsyncClient, api_prefix: str, zone: Zone):
    """A per-zone head count is a map of where the crowd is thickest.  That is
    not a public fact (Section 12)."""
    await ingest(client, api_prefix, reading(zone, HIGH_COUNT))

    response = await client.get(f"{api_prefix}/crowd/public")
    assert response.status_code == 200
    body = response.json()

    zone_out = body["zones"][0]
    assert zone_out["level"] == "high"
    assert zone_out["advice_mr"]
    for banned in ("person_count", "density", "flow", "occupancy_pct", "stagnation_index"):
        assert banned not in zone_out
    assert "no individual is identified" in body["notice"].lower()


async def test_a_pilgrim_cannot_read_the_detailed_view(
    client: AsyncClient, api_prefix: str, zone: Zone, pilgrim_token: str
):
    response = await client.get(
        f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {pilgrim_token}"}
    )
    assert response.status_code == 403
    assert "crowd:view_detail" in str(response.json()["error"]["details"])


async def test_an_unmeasured_zone_reads_as_unknown_not_as_clear(client: AsyncClient, api_prefix: str, zone: Zone):
    """The most important behaviour in the file.  A zone nobody is measuring
    must not render like an empty one — that is how someone walks into a crush."""
    response = await client.get(f"{api_prefix}/crowd/public")
    zone_out = response.json()["zones"][0]

    assert zone_out["level"] is None
    assert zone_out["is_stale"] is True
    assert "unknown" in zone_out["advice"].lower()
    assert "मोकळे आहे असे समजू नका" in zone_out["advice_mr"]


async def test_a_critical_zone_tells_pilgrims_to_stop(client: AsyncClient, api_prefix: str, zone: Zone):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT))

    response = await client.get(f"{api_prefix}/crowd/public")
    zone_out = response.json()["zones"][0]

    assert zone_out["level"] == "critical"
    assert "do not enter" in zone_out["advice"].lower()
    assert "जाऊ नका" in zone_out["advice_mr"]


async def test_zones_are_browsable_without_signing_in(client: AsyncClient, api_prefix: str, zone: Zone):
    """A pilgrim decides whether to travel before they create an account."""
    response = await client.get(f"{api_prefix}/zones")
    assert response.status_code == 200

    body = response.json()
    assert body[0]["code"] == "TC"
    assert body[0]["geometry"]["type"] == "Polygon"


# ---------------------------------------------------------------------------
# staleness and cameras
# ---------------------------------------------------------------------------
async def test_an_expired_snapshot_stops_being_served(
    session: AsyncSession, client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The Redis TTL is a safety property, not an optimisation: when the engine
    dies, the map must go to "no data" rather than freeze on a green zone."""
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))
    await crowd_service.invalidate(zone.id)

    # The database fallback is bounded by the same window, so a reading old
    # enough to have expired from the cache is old enough to stop serving.
    from sqlalchemy import text

    await session.execute(
        text("UPDATE density_readings SET time = time - INTERVAL '30 minutes' WHERE zone_id = :z"),
        {"z": zone.id},
    )
    await session.commit()

    response = await client.get(f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {officer_token}"})
    body = response.json()
    assert body["zones"] == []
    assert body["unknown_zones"] == ["TC"]


async def test_a_camera_heartbeat_updates_its_status(
    client: AsyncClient, api_prefix: str, camera: Camera, officer_token: str
):
    response = await client.post(
        f"{api_prefix}/ingest/heartbeat",
        json={"cameras": [{"camera_id": str(camera.id), "status": "degraded", "detail": "packet loss"}]},
        headers=AI_HEADERS,
    )
    assert response.status_code == 202
    assert response.json()["status_changed"] == 1

    listed = await client.get(f"{api_prefix}/cameras", headers={"Authorization": f"Bearer {officer_token}"})
    assert listed.json()[0]["status"] == "degraded"


async def test_a_camera_stream_url_is_never_returned(
    session: AsyncSession, client: AsyncClient, api_prefix: str, camera: Camera, officer_token: str
):
    """An RTSP URL with credentials in it is a way into the temple's camera
    network, not a display field."""
    camera.stream_url = "rtsp://admin:hunter2@10.0.0.5/stream1"
    await session.commit()

    response = await client.get(f"{api_prefix}/cameras", headers={"Authorization": f"Bearer {officer_token}"})
    body = response.text

    assert "hunter2" not in body
    assert "10.0.0.5" not in body
    assert response.json()[0]["has_stream"] is True


async def test_the_engine_config_carries_zones_and_calibrations(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera
):
    """What the AI engine pulls at boot.  It holds no state of its own, which
    is what makes restarting it safe."""
    response = await client.get(f"{api_prefix}/ingest/config", headers=AI_HEADERS)
    assert response.status_code == 200
    body = response.json()

    assert body["zones"][0]["code"] == "TC"
    assert body["zones"][0]["area_m2"] == AREA
    assert body["zones"][0]["cameras"][0]["name"] == "CAM-TC-01"
    assert body["zones"][0]["cameras"][0]["homography"] is None


# ---------------------------------------------------------------------------
# calibration through the API
# ---------------------------------------------------------------------------
@pytest.fixture
async def admin_token(make_user):
    user = await make_user(phone="9000000002", role=Role.ADMINISTRATOR, name="मंदिर प्रशासक")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=True)
    return token


CALIBRATION = {
    "points": [
        {"image": [200, 1000], "world": [0, 0]},
        {"image": [1720, 1000], "world": [60, 0]},
        {"image": [1180, 380], "world": [60, 20]},
        {"image": [740, 380], "world": [0, 20]},
    ],
    "frame_width": 1920,
    "frame_height": 1080,
}


async def test_calibration_stores_a_verified_homography(
    client: AsyncClient, api_prefix: str, camera: Camera, admin_token: str
):
    response = await client.post(
        f"{api_prefix}/cameras/{camera.id}/calibration",
        json=CALIBRATION,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert len(body["matrix"]) == 9
    assert body["residual_m"] < 0.01


async def test_calibration_can_derive_the_zone_area_from_the_outline(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, admin_token: str
):
    """A typed area and a clicked outline can disagree.  Only one of them was
    measured."""
    payload = {
        **CALIBRATION,
        "zone_polygon": [[200, 1000], [1720, 1000], [1180, 380], [740, 380]],
        "apply_zone_area": True,
        "note": "barricade rectangle, tape measured",
    }
    response = await client.post(
        f"{api_prefix}/cameras/{camera.id}/calibration",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 201

    body = response.json()
    assert body["computed_zone_area_m2"] == pytest.approx(1200.0, rel=1e-3)
    assert body["zone_area_updated"] is True


async def test_collinear_points_are_refused_with_a_usable_message(
    client: AsyncClient, api_prefix: str, camera: Camera, admin_token: str
):
    payload = {
        **CALIBRATION,
        "points": [
            {"image": [100, 500], "world": [0, 0]},
            {"image": [300, 500], "world": [10, 0]},
            {"image": [500, 500], "world": [20, 0]},
            {"image": [700, 900], "world": [30, 10]},
        ],
    }
    response = await client.post(
        f"{api_prefix}/cameras/{camera.id}/calibration",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "CALIBRATION_INVALID"
    assert body["message_mr"]


async def test_a_security_officer_cannot_calibrate(
    client: AsyncClient, api_prefix: str, camera: Camera, officer_token: str
):
    """`crowd:calibrate` is Administrator and above: changing a homography
    changes every density figure the camera will ever produce."""
    response = await client.post(
        f"{api_prefix}/cameras/{camera.id}/calibration",
        json=CALIBRATION,
        headers={"Authorization": f"Bearer {officer_token}"},
    )
    assert response.status_code == 403


async def test_calibration_is_audited_with_the_points_that_produced_it(
    client: AsyncClient, api_prefix: str, camera: Camera, admin_token: str, session: AsyncSession
):
    await client.post(
        f"{api_prefix}/cameras/{camera.id}/calibration",
        json={**CALIBRATION, "note": "north barricade"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    from sqlalchemy import select

    from app.models import AuditLog

    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "camera.calibrated"))
    assert entry is not None
    assert entry.meta["note"] == "north barricade"
    assert len(entry.meta["image_points"]) == 4
    assert entry.meta["residual_m"] < 0.01


async def test_re_surveying_a_zone_requires_a_reason_and_is_audited(
    client: AsyncClient, api_prefix: str, zone: Zone, admin_token: str, session: AsyncSession
):
    response = await client.patch(
        f"{api_prefix}/zones/TC",
        json={"area_m2": 1500.0, "reason": "re-surveyed after the barricades moved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["area_m2"] == 1500.0

    from sqlalchemy import select

    from app.models import AuditLog

    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "zone.updated"))
    assert entry is not None
    assert entry.meta["before"]["area_m2"] == AREA
    assert "barricades" in entry.meta["reason"]


async def test_changing_the_area_changes_every_subsequent_density(
    client: AsyncClient, api_prefix: str, zone: Zone, admin_token: str, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, 2400))
    await client.patch(
        f"{api_prefix}/zones/TC",
        json={"area_m2": 2400.0, "reason": "re-surveyed"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    await ingest(client, api_prefix, reading(zone, 2400))

    response = await client.get(f"{api_prefix}/crowd/live", headers={"Authorization": f"Bearer {officer_token}"})
    assert response.json()["zones"][0]["density"] == pytest.approx(1.0, rel=1e-3)


# ---------------------------------------------------------------------------
# time series
# ---------------------------------------------------------------------------
async def test_the_series_includes_the_newest_bucket(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The continuous aggregate lags a minute behind.  An operator watching a
    zone climb needs the newest bucket most of all."""
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT))

    response = await client.get(
        f"{api_prefix}/zones/TC/series?minutes=30", headers={"Authorization": f"Bearer {officer_token}"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["zone_code"] == "TC"
    assert len(body["points"]) >= 1
    assert body["points"][-1]["peak_level"] == "critical"


async def test_an_unknown_zone_is_a_clean_404(client: AsyncClient, api_prefix: str, officer_token: str):
    response = await client.get(
        f"{api_prefix}/zones/NOPE/series", headers={"Authorization": f"Bearer {officer_token}"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ZONE_NOT_FOUND"
    assert response.json()["error"]["message_mr"]
