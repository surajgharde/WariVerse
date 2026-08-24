"""The KPI strip's honesty rules (Section 4/M3).

These are pure-function tests — no database, no Redis — because the rules they
encode are the ones that must never regress quietly, and a rule guarded only by
an integration test is a rule that stops being checked the first time somebody
runs the suite without Docker.

The rule under test throughout: **a number we are not measuring is `None`,
never `0`.** Every assertion here is some version of "these two situations look
different on screen".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.config import settings
from app.core.security import now_utc
from app.models import Camera, Zone
from app.models.crowd import DensityLevel
from app.services import command_service
from app.services.command_service import _band, _kpi_cameras, _kpi_pilgrims, _kpi_throughput, _kpi_wait, _partition
from app.services.crowd_service import ZoneSnapshot


def make_zone(code: str, *, capacity: int = 1000, zone_type: str = "temple_core") -> Zone:
    zone = Zone(
        code=code,
        name=f"Zone {code}",
        name_mr=f"झोन {code}",
        area_m2=1000.0,
        capacity_persons=capacity,
        zone_type=zone_type,
    )
    zone.id = uuid.uuid4()
    return zone


def snapshot(zone: Zone, count: int, *, age_seconds: float = 0.0, confidence: float = 1.0) -> ZoneSnapshot:
    density = count / zone.area_m2
    return ZoneSnapshot(
        zone_id=zone.id,
        zone_code=zone.code,
        zone_name=zone.name,
        zone_name_mr=zone.name_mr,
        person_count=count,
        density=density,
        level=DensityLevel.SAFE,
        flow_dx=0.0,
        flow_dy=0.0,
        stagnation_index=0.0,
        counterflow_ratio=0.0,
        confidence=confidence,
        source="sim",
        camera_count=1,
        observed_at=now_utc() - timedelta(seconds=age_seconds),
        area_m2=zone.area_m2,
        capacity_persons=zone.capacity_persons,
    )


# ---------------------------------------------------------------------------
# the core rule
# ---------------------------------------------------------------------------
def test_no_readings_reports_unknown_rather_than_zero_pilgrims():
    """The single most important assertion in this file.

    An empty temple and a dead pipeline both produce "no readings". If this
    returned 0 the strip would tell an operator the complex is empty at the
    exact moment it stopped being able to see it.
    """
    zone = make_zone("TC")
    zones = {zone.id: zone}
    fresh = _partition(zones, [])

    kpi = _kpi_pilgrims(fresh, zones, at=now_utc())

    assert kpi.value is None
    assert kpi.state == "unknown"
    assert kpi.source == "unavailable"
    assert kpi.note and "not zero" in kpi.note
    assert kpi.note_mr  # Marathi is not optional (Section 10)
    assert kpi.detail["zones_unknown"] == 1


def test_a_genuinely_empty_zone_still_reports_zero():
    """The other half of the rule: a zone that *is* reporting zero people is a
    measurement, and must not be laundered into "unknown"."""
    zone = make_zone("TC")
    zones = {zone.id: zone}
    fresh = _partition(zones, [snapshot(zone, 0)])

    kpi = _kpi_pilgrims(fresh, zones, at=now_utc())

    assert kpi.value == 0.0
    assert kpi.state == "ok"
    assert kpi.source != "unavailable"


def test_stale_readings_are_excluded_from_the_total_and_declared():
    """A headcount must not silently mix a live number with a four-minute-old
    one — no badge can tell the operator which half is which."""
    live = make_zone("A")
    cold = make_zone("B")
    zones = {live.id: live, cold.id: cold}
    fresh = _partition(
        zones,
        [
            snapshot(live, 500),
            snapshot(cold, 400, age_seconds=settings.stale_reading_seconds + 30),
        ],
    )

    kpi = _kpi_pilgrims(fresh, zones, at=now_utc())

    assert kpi.value == 500.0, "the stale 400 must not be added in"
    assert kpi.detail["zones_stale"] == 1
    assert kpi.note and "higher" in kpi.note, "operator is told the real figure exceeds what is shown"


def test_partial_coverage_is_declared_even_when_every_reading_is_fresh():
    counted = make_zone("A")
    silent = make_zone("B")
    zones = {counted.id: counted, silent.id: silent}
    fresh = _partition(zones, [snapshot(counted, 300)])

    kpi = _kpi_pilgrims(fresh, zones, at=now_utc())

    assert kpi.value == 300.0
    assert kpi.detail["unknown_zone_codes"] == ["B"]
    assert kpi.note is not None


def test_occupancy_drives_the_state_band():
    zone = make_zone("TC", capacity=1000)
    zones = {zone.id: zone}

    def state_for(count: int) -> str:
        return _kpi_pilgrims(_partition(zones, [snapshot(zone, count)]), zones, at=now_utc()).state

    assert state_for(500) == "ok"
    assert state_for(750) == "watch"
    assert state_for(950) == "breach"


# ---------------------------------------------------------------------------
# wait time
# ---------------------------------------------------------------------------
def test_wait_uses_observed_throughput_not_the_plan():
    queue = make_zone("Q", zone_type="queue")
    zones = {queue.id: queue}
    fresh = _partition(zones, [snapshot(queue, 3000)])

    kpi = _kpi_wait(fresh, zones, observed_per_hour=1500.0, at=now_utc())

    # 3000 people at 1500/hour is two hours, not the 30 minutes the 6000/hour
    # plan would have promised.
    assert kpi.value == 120.0
    assert kpi.state == "watch"
    assert kpi.detail["people_ahead"] == 3000


def test_wait_is_unknown_when_the_gate_has_stopped():
    """Zero throughput is a stalled gate, not an infinite wait — and certainly
    not a zero-minute one."""
    queue = make_zone("Q", zone_type="queue")
    zones = {queue.id: queue}
    fresh = _partition(zones, [snapshot(queue, 3000)])

    kpi = _kpi_wait(fresh, zones, observed_per_hour=0.0, at=now_utc())

    assert kpi.value is None
    assert kpi.state == "unknown"
    assert kpi.note and "gate" in kpi.note


def test_wait_ignores_non_queue_zones():
    """People standing in the temple core are not queueing behind you."""
    core = make_zone("TC", zone_type="temple_core")
    zones = {core.id: core}
    fresh = _partition(zones, [snapshot(core, 5000)])

    kpi = _kpi_wait(fresh, zones, observed_per_hour=1500.0, at=now_utc())

    assert kpi.value is None
    assert kpi.note and "queue" in kpi.note


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------
def test_throughput_is_judged_against_the_plan_for_this_window():
    """A quiet 05:00 hour is not a failure. Comparing it against the full-day
    6000/hour target would flag every morning red and train operators to ignore
    the card."""
    kpi = _kpi_throughput(
        observed_per_hour=950.0,
        planned_per_hour=1000.0,
        target_per_hour=6000,
        window_minutes=30,
        at=now_utc(),
    )

    assert kpi.state == "ok"
    assert kpi.target == 1000.0


def test_throughput_below_eighty_percent_of_plan_is_a_breach():
    """The same 20% line `reslot_deviation_pct` uses before it starts moving
    people's slots."""
    kpi = _kpi_throughput(
        observed_per_hour=700.0,
        planned_per_hour=1000.0,
        target_per_hour=6000,
        window_minutes=30,
        at=now_utc(),
    )
    assert kpi.state == "breach"


def test_throughput_unmeasurable_reports_unknown_and_keeps_the_target():
    kpi = _kpi_throughput(
        observed_per_hour=None,
        planned_per_hour=None,
        target_per_hour=6000,
        window_minutes=30,
        at=now_utc(),
    )
    assert kpi.value is None
    assert kpi.state == "unknown"
    assert kpi.target == 6000.0, "the target is config, and stays readable even with no actual"


# ---------------------------------------------------------------------------
# cameras
# ---------------------------------------------------------------------------
def _camera(status: str, *, calibrated: bool = True) -> Camera:
    return Camera(
        zone_id=uuid.uuid4(),
        name=f"CAM-{status}",
        status=status,
        homography_matrix={"matrix": [1, 0, 0, 0, 1, 0, 0, 0, 1]} if calibrated else None,
    )


def test_all_cameras_online_is_ok():
    kpi = _kpi_cameras([_camera("online"), _camera("online")], at=now_utc())
    assert kpi.value == 2.0
    assert kpi.target == 2.0
    assert kpi.state == "ok"


def test_one_camera_down_is_watch_and_a_fifth_down_is_breach():
    watch = _kpi_cameras([_camera("online")] * 9 + [_camera("offline")], at=now_utc())
    assert watch.state == "watch"

    breach = _kpi_cameras([_camera("online")] * 7 + [_camera("offline")] * 3, at=now_utc())
    assert breach.state == "breach"


def test_no_cameras_registered_is_unknown_not_zero_online():
    kpi = _kpi_cameras([], at=now_utc())
    assert kpi.value is None
    assert kpi.state == "unknown"
    assert kpi.note and "manual or simulated" in kpi.note


def test_uncalibrated_cameras_are_called_out():
    """An uncalibrated camera reports "online" while contributing a density
    derived from no measured ground plane — Section 4/M2's "density is fiction"
    warning, surfaced where an operator will see it."""
    kpi = _kpi_cameras([_camera("online"), _camera("online", calibrated=False)], at=now_utc())

    assert kpi.value == 2.0
    assert kpi.detail["uncalibrated"] == 1
    assert kpi.note and "estimates" in kpi.note


# ---------------------------------------------------------------------------
# banding
# ---------------------------------------------------------------------------
def test_band_inverted_treats_lower_as_worse():
    # Throughput: a ratio of 0.7 against plan is bad, 1.1 is fine.
    assert _band(0.7, watch=0.9, breach=0.8, inverted=True) == "breach"
    assert _band(0.85, watch=0.9, breach=0.8, inverted=True) == "watch"
    assert _band(1.1, watch=0.9, breach=0.8, inverted=True) == "ok"


def test_band_normal_treats_higher_as_worse():
    assert _band(95.0, watch=70.0, breach=90.0) == "breach"
    assert _band(75.0, watch=70.0, breach=90.0) == "watch"
    assert _band(10.0, watch=70.0, breach=90.0) == "ok"


# ---------------------------------------------------------------------------
# the strip as a whole
# ---------------------------------------------------------------------------
def test_kpi_without_intake_declares_itself_unavailable():
    """A KPI whose intake does not exist must not read as a confident zero —
    "0 breaches pending" says the field is quiet.

    Open incidents used to be built this way too. Phase 5 gave it a real
    count; breaches wait for Phase 6.
    """
    breaches = command_service._unavailable(
        "breaches_pending_review",
        "Breaches pending review",
        "पुनरावलोकन बाकी उल्लंघने",
        "count",
        "Phase 6.",
        "टप्पा 6.",
    )
    assert breaches.value is None
    assert breaches.source == "unavailable"
    assert breaches.state == "unknown"
