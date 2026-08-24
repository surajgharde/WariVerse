"""The "what changed in the last 15 minutes" strip (Section 4/M3, last rule).

Pure-function tests over `_alert_change_items` — the part that decides what one
alert contributes to an operator's catch-up. No database needed.

The behaviour being pinned down: an alert is not one line in the digest, it is
however many things happened to it inside the window. An alert raised at 14:02,
escalated at 14:03 and acknowledged at 14:05 is three lines, because collapsing
it to one loses the fact that it sat unacknowledged for three minutes — which
is the fact a post-Wari review will ask about.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.security import now_utc
from app.models import Alert, Zone
from app.services.command_service import _alert_change_items


def make_zone(code: str = "TC") -> Zone:
    zone = Zone(
        code=code,
        name="Temple Core",
        name_mr="मंदिर गाभारा",
        area_m2=1200.0,
        capacity_persons=2400,
        zone_type="temple_core",
    )
    zone.id = uuid.uuid4()
    return zone


def make_alert(
    *,
    created_at,
    alert_type: str = "density_critical",
    severity: str = "critical",
    acknowledged_at=None,
    escalated_at=None,
    resolved_at=None,
    escalation_level: int = 0,
    zone_id=None,
) -> Alert:
    alert = Alert(
        type=alert_type,
        severity=severity,
        zone_id=zone_id,
        trigger_metric="density",
        trigger_value=5.4,
        threshold_value=5.0,
        confidence=0.9,
        observed_at=created_at,
        status="open",
        acknowledged_at=acknowledged_at,
        escalated_at=escalated_at,
        escalation_level=escalation_level,
        resolved_at=resolved_at,
    )
    alert.id = uuid.uuid4()
    alert.created_at = created_at
    return alert


def test_one_alert_contributes_a_line_per_lifecycle_step():
    until = now_utc()
    since = until - timedelta(minutes=15)
    zone = make_zone()

    alert = make_alert(
        created_at=since + timedelta(minutes=2),
        acknowledged_at=since + timedelta(minutes=5),
        escalated_at=since + timedelta(minutes=3),
        escalation_level=1,
        zone_id=zone.id,
    )

    items = _alert_change_items(alert, zone, since=since, until=until)
    kinds = {i.kind for i in items}

    assert kinds == {"alert_raised", "alert_escalated", "alert_acknowledged"}
    assert all(i.ref_type == "alert" and i.ref_id == alert.id for i in items)
    assert all(i.zone_code == "TC" for i in items)
    assert all(i.summary_mr for i in items), "Marathi is not optional"


def test_steps_outside_the_window_are_not_reported():
    """An alert raised an hour ago and acknowledged just now contributes the
    acknowledgement only — the raise is old news the operator already saw."""
    until = now_utc()
    since = until - timedelta(minutes=15)

    alert = make_alert(
        created_at=until - timedelta(hours=1),
        acknowledged_at=since + timedelta(minutes=1),
    )

    items = _alert_change_items(alert, None, since=since, until=until)

    assert [i.kind for i in items] == ["alert_acknowledged"]


def test_an_escalation_is_always_critical_regardless_of_the_alert_severity():
    """Escalation means nobody answered. That is a control-room fact, not a
    crowd fact, so it does not inherit the alert's own severity."""
    until = now_utc()
    since = until - timedelta(minutes=15)

    alert = make_alert(
        created_at=since + timedelta(minutes=1),
        severity="warning",
        escalated_at=since + timedelta(minutes=2),
        escalation_level=2,
    )

    items = _alert_change_items(alert, None, since=since, until=until)
    escalation = next(i for i in items if i.kind == "alert_escalated")
    assert escalation.severity == "critical"


def test_a_camera_offline_alert_reads_as_a_camera_status_change():
    """Cameras keep only their current status — no history. The camera_offline
    alert *is* the persisted record of the transition, so it is classified as
    `camera_status` rather than reported twice under two kinds."""
    until = now_utc()
    since = until - timedelta(minutes=15)
    zone = make_zone("C")

    alert = make_alert(
        created_at=since + timedelta(minutes=1),
        alert_type="camera_offline",
        severity="warning",
        zone_id=zone.id,
    )

    items = _alert_change_items(alert, zone, since=since, until=until)

    assert [i.kind for i in items] == ["camera_status"]
    assert "offline" in items[0].summary
    assert "alert_raised" not in {i.kind for i in items}


def test_a_camera_coming_back_reads_as_a_camera_status_change_too():
    until = now_utc()
    since = until - timedelta(minutes=15)
    zone = make_zone("C")

    alert = make_alert(
        created_at=until - timedelta(hours=2),
        alert_type="camera_offline",
        severity="warning",
        resolved_at=since + timedelta(minutes=4),
        zone_id=zone.id,
    )

    items = _alert_change_items(alert, zone, since=since, until=until)

    assert [i.kind for i in items] == ["camera_status"]
    assert "back online" in items[0].summary


def test_an_alert_with_no_zone_still_produces_a_readable_line():
    """Not every alert is zonal — a throughput drop is system-wide. The summary
    must not read "... in None"."""
    until = now_utc()
    since = until - timedelta(minutes=15)

    alert = make_alert(created_at=since + timedelta(minutes=1), alert_type="throughput_drop")
    items = _alert_change_items(alert, None, since=since, until=until)

    assert items[0].zone_code is None
    assert "None" not in items[0].summary
    assert "None" not in items[0].summary_mr
