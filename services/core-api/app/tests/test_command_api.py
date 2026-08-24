"""The command-centre endpoints end to end (Section 4/M3, Phase 4).

    GET /command/kpis   /command/changes   /command/replay   /command/config

Needs Postgres and Redis (`docker compose up -d db redis`).

The unit-level rules live in `test_command_kpis.py` and `test_command_digest.py`;
this file checks the parts that only exist once there is a database underneath —
the permission gate, the replay window assembled from real rollups, and the
digest reading real alerts.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token, now_utc
from app.models import Camera, Zone
from app.services import alert_service, config_service

pytestmark = [pytest.mark.db, pytest.mark.redis]

AI_HEADERS = {"x-ai-service-token": "test-ai-service-token"}

AREA = 1200.0
SAFE_COUNT = 1_200      # 1.0 p/m²
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


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def ingest(client: AsyncClient, api_prefix: str, *readings: dict) -> None:
    response = await client.post(
        f"{api_prefix}/ingest/density",
        json={"source": "sim", "readings": list(readings)},
        headers=AI_HEADERS,
    )
    assert response.status_code == 202, response.text


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["kpis", "changes", "replay", "config"])
async def test_a_pilgrim_cannot_reach_the_command_centre(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, path: str
):
    """The KPI strip is a headcount in aggregate form. A role that may not read
    the detailed map may not read a total derived from it either."""
    response = await client.get(f"{api_prefix}/command/{path}", headers=auth(pilgrim_token))
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["kpis", "changes", "replay", "config"])
async def test_the_command_centre_requires_a_token(client: AsyncClient, api_prefix: str, path: str):
    response = await client.get(f"{api_prefix}/command/{path}")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# kpis
# ---------------------------------------------------------------------------
async def test_the_strip_returns_all_six_kpis(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    response = await client.get(f"{api_prefix}/command/kpis", headers=auth(officer_token))
    assert response.status_code == 200
    body = response.json()

    keys = [k["key"] for k in body["kpis"]]
    assert keys == [
        "pilgrims_in_complex",
        "current_wait_minutes",
        "darshan_per_hour",
        "open_incidents",
        "breaches_pending_review",
        "cameras_online",
    ]
    assert all(k["label_mr"] for k in body["kpis"]), "Marathi labels are not optional"


async def test_the_headcount_reflects_a_live_reading(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    body = (await client.get(f"{api_prefix}/command/kpis", headers=auth(officer_token))).json()
    pilgrims = next(k for k in body["kpis"] if k["key"] == "pilgrims_in_complex")

    assert pilgrims["value"] == SAFE_COUNT
    assert pilgrims["is_stale"] is False
    assert pilgrims["source"] == "sim"


async def test_with_nothing_ingested_the_headcount_is_null_not_zero(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    """The whole point of the strip, checked against a real database: a zone
    that exists but has never reported must not read as an empty temple."""
    body = (await client.get(f"{api_prefix}/command/kpis", headers=auth(officer_token))).json()
    pilgrims = next(k for k in body["kpis"] if k["key"] == "pilgrims_in_complex")

    assert pilgrims["value"] is None
    assert pilgrims["state"] == "unknown"
    assert body["unknown_count"] >= 1


async def test_the_camera_kpi_counts_the_roster(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, session: AsyncSession, officer_token: str
):
    session.add(Camera(zone_id=zone.id, name="CAM-TC-02", status="offline"))
    await session.commit()

    body = (await client.get(f"{api_prefix}/command/kpis", headers=auth(officer_token))).json()
    cameras = next(k for k in body["kpis"] if k["key"] == "cameras_online")

    assert cameras["value"] == 1.0
    assert cameras["target"] == 2.0
    assert cameras["detail"]["offline"] == 1


# ---------------------------------------------------------------------------
# changes
# ---------------------------------------------------------------------------
async def test_a_raised_alert_appears_in_the_digest(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT))

    body = (await client.get(f"{api_prefix}/command/changes", headers=auth(officer_token))).json()

    raised = [i for i in body["items"] if i["kind"] == "alert_raised"]
    assert raised, "a critical density reading should show up in the catch-up strip"
    assert raised[0]["severity"] == "critical"
    assert raised[0]["zone_code"] == "TC"
    assert raised[0]["summary_mr"]


async def test_the_digest_is_ordered_worst_first(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, CRITICAL_COUNT))

    body = (await client.get(f"{api_prefix}/command/changes", headers=auth(officer_token))).json()
    rank = {"critical": 0, "warning": 1, "info": 2}
    severities = [rank[i["severity"]] for i in body["items"]]

    assert severities == sorted(severities), "an operator's eye lands on the worst line first"


async def test_an_empty_window_is_an_empty_digest_not_an_error(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    body = (await client.get(f"{api_prefix}/command/changes?minutes=1", headers=auth(officer_token))).json()

    assert body["items"] == []
    assert body["truncated"] is False


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
async def test_replay_returns_frames_for_ingested_readings(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    response = await client.get(f"{api_prefix}/command/replay?minutes=15", headers=auth(officer_token))
    assert response.status_code == 200
    body = response.json()

    assert body["step_seconds"] == 60
    assert body["zone_codes"] == ["TC"]
    assert body["frames"], "the minute just ingested should be a frame"
    assert body["frames"][-1]["zones"][0]["zone_code"] == "TC"
    assert body["note_mr"]


async def test_replay_names_zones_that_were_not_measured(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, session: AsyncSession, officer_token: str
):
    """A zone with no reading in a minute is named in `unknown_zones` rather
    than held at its previous colour — a replay that stays green through an
    outage lies about the one interval anybody will ask about."""
    silent = Zone(
        code="GH",
        name="Ghat",
        name_mr="घाट",
        geom="SRID=4326;POLYGON((75.34 17.679, 75.341 17.679, 75.341 17.68, 75.34 17.68, 75.34 17.679))",
        area_m2=800.0,
        capacity_persons=1600,
        zone_type="ghat",
    )
    session.add(silent)
    await session.commit()

    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    body = (await client.get(f"{api_prefix}/command/replay?minutes=15", headers=auth(officer_token))).json()

    assert set(body["zone_codes"]) == {"TC", "GH"}
    assert "GH" in body["frames"][-1]["unknown_zones"]


async def test_replay_can_be_narrowed_to_named_zones(
    client: AsyncClient, api_prefix: str, zone: Zone, camera: Camera, officer_token: str
):
    await ingest(client, api_prefix, reading(zone, SAFE_COUNT))

    body = (
        await client.get(f"{api_prefix}/command/replay?minutes=15&zones=TC", headers=auth(officer_token))
    ).json()

    assert body["zone_codes"] == ["TC"]


async def test_replay_window_is_capped(client: AsyncClient, api_prefix: str, officer_token: str):
    """An unbounded scrubber is a way to ask the database for a week of
    ten-second readings in one request."""
    response = await client.get(f"{api_prefix}/command/replay?minutes=100000", headers=auth(officer_token))
    assert response.status_code == 422


async def test_a_quiet_window_replays_as_no_frames(
    client: AsyncClient, api_prefix: str, zone: Zone, officer_token: str
):
    body = (await client.get(f"{api_prefix}/command/replay?minutes=5", headers=auth(officer_token))).json()

    assert body["frames"] == []
    assert body["zone_codes"] == ["TC"], "the legend stays stable even with no data"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
async def test_config_serves_the_numbers_the_console_must_not_guess(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    """A console counting to 60 while the server escalates at 90 would turn an
    alert card red before anything happened."""
    response = await client.get(f"{api_prefix}/command/config", headers=auth(officer_token))
    assert response.status_code == 200
    body = response.json()

    assert body["alert_escalate_seconds"] == 60
    assert body["alert_page_seconds"] == 180
    assert body["stale_reading_seconds"] == 90
    assert body["density_thresholds"] == {"safe": 2.0, "moderate": 3.5, "high": 5.0}
    assert body["crowd_source"] in {"live", "video", "sim"}


async def test_config_carries_server_time_for_clock_skew(
    client: AsyncClient, api_prefix: str, officer_token: str
):
    """An operator's laptop running ninety seconds fast would render every live
    reading as stale, and "everything is grey" looks exactly like a dead
    pipeline."""
    from datetime import datetime

    body = (await client.get(f"{api_prefix}/command/config", headers=auth(officer_token))).json()
    server_time = datetime.fromisoformat(body["server_time"])

    assert abs((now_utc() - server_time).total_seconds()) < timedelta(minutes=1).total_seconds()
