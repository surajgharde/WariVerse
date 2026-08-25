"""What the command centre reads (Section 4/M3).

Three questions an operator asks, in the order they ask them:

1. *Is anything wrong right now?*      -> `kpi_strip`
2. *What did I miss?*                  -> `change_digest`
3. *How did we get here?*              -> `replay_window`

Everything in this module obeys one rule, stated in `schemas/command.py` and
repeated here because it is the rule that gets broken under deadline pressure:
**a number we are not measuring is `None`, never `0`.**  An empty temple and a
dead pipeline produce the same zero, and a strip that renders them the same way
is how an operator stands down during a surge.

Open incidents and breaches pending review were both placeholders returning
`None` with a "not until Phase N" note, and both are now real counts. Note what
did *not* change when they became live: each is still `None` when the count
cannot be *read*, and `0` only when the thing being counted is genuinely empty.
Turning a placeholder into a live number is the moment that distinction is
easiest to lose, because `0` finally looks like a legitimate answer.

The breach card carries one extra rule of its own. It counts events *awaiting
review*, not breaches — an event is a detection and becomes a finding only when
a human says so (Section 4/M5) — and it reports `breach` state outright when the
ledger's hash chain fails to verify, whatever the backlog is. A pending count
read off a ledger that does not verify is a number nobody should act on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Alert, Camera, Zone, classify_density
from app.models.crowd import DensityLevel
from app.schemas.command import (
    ChangeDigest,
    ChangeItem,
    ChangeKind,
    Kpi,
    KpiState,
    KpiStrip,
    ReplayFrame,
    ReplayWindow,
    ReplayZoneState,
)
from app.services import (
    alert_service,
    breach_service,
    config_service,
    crowd_service,
    incident_service,
    pass_service,
)
from app.services.crowd_service import ZoneSnapshot

logger = get_logger(__name__)

#: The digest's default look-back.  Section 4/M3 asks for fifteen minutes
#: because that is roughly how long a walkabout takes.
DIGEST_MINUTES = 15
DIGEST_LIMIT = 60

#: Replay resolution.  The continuous aggregate buckets at one minute, so this
#: is not a choice so much as a statement of what the data actually is — asking
#: for ten-second frames would interpolate, and an interpolated replay of a
#: crush is evidence of nothing.
REPLAY_STEP_SECONDS = 60
REPLAY_MAX_MINUTES = 360

#: Zone types whose occupants are queueing for darshan.  The wait estimate is
#: only as good as this list, so it is here and named rather than inline.
_QUEUE_ZONE_TYPES = ("queue", "corridor")


# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Fresh:
    """The zone readings a KPI is allowed to be computed from."""

    usable: list[ZoneSnapshot]
    stale: list[ZoneSnapshot]
    unknown_codes: list[str]

    @property
    def oldest_at(self) -> datetime | None:
        if not self.usable:
            return None
        return min(s.observed_at for s in self.usable)

    @property
    def confidence(self) -> float:
        if not self.usable:
            return 0.0
        return round(sum(s.confidence for s in self.usable) / len(self.usable), 3)


def _partition(zones: dict[uuid.UUID, Zone], snapshots: list[ZoneSnapshot]) -> _Fresh:
    """Split readings into usable, stale and never-seen.

    Stale readings are deliberately excluded from every total rather than
    included with a badge.  A headcount that silently mixes a live number with
    one from four minutes ago is not a headcount, and no badge on the card can
    tell the operator which half is which.
    """
    usable = [s for s in snapshots if not s.is_stale]
    stale = [s for s in snapshots if s.is_stale]
    seen = {s.zone_id for s in snapshots}
    unknown = sorted(z.code for zid, z in zones.items() if zid not in seen)
    return _Fresh(usable=usable, stale=stale, unknown_codes=unknown)


def _age(as_of: datetime | None, *, at: datetime) -> tuple[float | None, bool]:
    if as_of is None:
        return None, False
    age = max(0.0, (at - as_of).total_seconds())
    return round(age, 1), age > settings.stale_reading_seconds


def _band(value: float, *, watch: float, breach: float, inverted: bool = False) -> KpiState:
    """Three-way threshold. `inverted` for metrics where *lower* is worse."""
    if inverted:
        if value <= breach:
            return "breach"
        return "watch" if value <= watch else "ok"
    if value >= breach:
        return "breach"
    return "watch" if value >= watch else "ok"


def _unavailable(
    key: str,
    label: str,
    label_mr: str,
    unit: str,
    note: str,
    note_mr: str,
) -> Kpi:
    return Kpi(
        key=key,
        label=label,
        label_mr=label_mr,
        value=None,
        unit=unit,  # type: ignore[arg-type]
        source="unavailable",
        confidence=0.0,
        state="unknown",
        note=note,
        note_mr=note_mr,
    )


def _kpi_pilgrims(fresh: _Fresh, zones: dict[uuid.UUID, Zone], *, at: datetime) -> Kpi:
    as_of = fresh.oldest_at
    age, stale = _age(as_of, at=at)
    detail = {
        "zones_counted": len(fresh.usable),
        "zones_stale": len(fresh.stale),
        "zones_unknown": len(fresh.unknown_codes),
        "unknown_zone_codes": fresh.unknown_codes,
    }

    if not fresh.usable:
        return Kpi(
            key="pilgrims_in_complex",
            label="Pilgrims in complex",
            label_mr="संकुलातील भाविक",
            value=None,
            unit="persons",
            source="unavailable",
            confidence=0.0,
            state="unknown",
            detail=detail,
            note="No zone is reporting. This is not zero pilgrims — it is no measurement.",
            note_mr="कोणताही झोन माहिती देत नाही. याचा अर्थ भाविक नाहीत असा नाही — मोजणीच होत नाही.",
        )

    total = sum(s.person_count for s in fresh.usable)
    capacity = sum(
        zones[s.zone_id].capacity_persons for s in fresh.usable if s.zone_id in zones
    )
    detail["capacity_of_counted_zones"] = capacity

    state: KpiState = "unknown"
    if capacity > 0:
        occupancy = 100.0 * total / capacity
        detail["occupancy_pct"] = round(occupancy, 1)
        state = _band(occupancy, watch=70.0, breach=90.0)

    note = note_mr = None
    if fresh.unknown_codes or fresh.stale:
        missing = len(fresh.unknown_codes) + len(fresh.stale)
        note = f"Partial count — {missing} zone(s) not reporting. The real figure is higher."
        note_mr = f"अपूर्ण मोजणी — {missing} झोनकडून माहिती नाही. खरा आकडा यापेक्षा जास्त आहे."

    return Kpi(
        key="pilgrims_in_complex",
        label="Pilgrims in complex",
        label_mr="संकुलातील भाविक",
        value=float(total),
        unit="persons",
        as_of=as_of,
        age_seconds=age,
        is_stale=stale,
        source=settings.crowd_source,
        confidence=fresh.confidence,
        state=state,
        detail=detail,
        note=note,
        note_mr=note_mr,
    )


def _kpi_wait(
    fresh: _Fresh,
    zones: dict[uuid.UUID, Zone],
    *,
    observed_per_hour: float | None,
    at: datetime,
) -> Kpi:
    """Minutes for someone joining the back of the queue right now.

    Queue length divided by the rate the gate is *actually* clearing, not the
    rate it is supposed to clear.  The planned figure would produce a shorter,
    more comfortable number on exactly the day it is most wrong.
    """
    queued = [
        s
        for s in fresh.usable
        if s.zone_id in zones and zones[s.zone_id].zone_type in _QUEUE_ZONE_TYPES
    ]
    detail: dict[str, object] = {
        "queue_zone_types": list(_QUEUE_ZONE_TYPES),
        "queue_zones_counted": len(queued),
        "observed_throughput_per_hour": observed_per_hour,
    }

    if not queued or not observed_per_hour or observed_per_hour <= 0:
        reason = "no queue zone is reporting" if not queued else "the gate has cleared nobody in the last half hour"
        reason_mr = (
            "कोणताही रांग-झोन माहिती देत नाही"
            if not queued
            else "गेल्या अर्ध्या तासात गेटमधून कोणीही गेलेले नाही"
        )
        return Kpi(
            key="current_wait_minutes",
            label="Current wait",
            label_mr="सध्याची प्रतीक्षा",
            value=None,
            unit="minutes",
            source="unavailable",
            confidence=0.0,
            state="unknown",
            detail=detail,
            note=f"Cannot be estimated — {reason}.",
            note_mr=f"अंदाज काढता येत नाही — {reason_mr}.",
        )

    ahead = sum(s.person_count for s in queued)
    minutes = round(60.0 * ahead / observed_per_hour, 1)
    detail["people_ahead"] = ahead

    as_of = min(s.observed_at for s in queued)
    age, stale = _age(as_of, at=at)

    return Kpi(
        key="current_wait_minutes",
        label="Current wait",
        label_mr="सध्याची प्रतीक्षा",
        value=minutes,
        unit="minutes",
        as_of=as_of,
        age_seconds=age,
        is_stale=stale,
        source="derived",
        confidence=round(sum(s.confidence for s in queued) / len(queued), 3),
        state=_band(minutes, watch=120.0, breach=240.0),
        detail=detail,
        note="Queue length divided by the throughput actually observed, not the planned rate.",
        note_mr="रांगेतील संख्या भागिले प्रत्यक्ष निरीक्षण केलेला वेग — नियोजित वेग नव्हे.",
    )


def _kpi_throughput(
    *,
    observed_per_hour: float | None,
    planned_per_hour: float | None,
    target_per_hour: int,
    window_minutes: int,
    at: datetime,
) -> Kpi:
    detail: dict[str, object] = {
        "window_minutes": window_minutes,
        "planned_in_window_per_hour": planned_per_hour,
        "configured_target_per_hour": target_per_hour,
    }

    if observed_per_hour is None:
        return Kpi(
            key="darshan_per_hour",
            label="Darshan / hour",
            label_mr="दर तासाला दर्शन",
            value=None,
            unit="per_hour",
            target=float(target_per_hour),
            source="unavailable",
            confidence=0.0,
            state="unknown",
            detail=detail,
            note="No slots were scheduled in this window, so there is no rate to report.",
            note_mr="या कालावधीत कोणतेही स्लॉट नियोजित नव्हते, त्यामुळे वेग सांगता येत नाही.",
        )

    # Measure against what was planned *for this window* where that exists —
    # comparing an 05:00 hour against the full-day target would flag every
    # quiet morning as a failure.
    reference = planned_per_hour if planned_per_hour else float(target_per_hour)
    ratio = observed_per_hour / reference if reference > 0 else 0.0
    detail["ratio_to_plan"] = round(ratio, 3)

    return Kpi(
        key="darshan_per_hour",
        label="Darshan / hour",
        label_mr="दर तासाला दर्शन",
        value=round(observed_per_hour, 1),
        unit="per_hour",
        target=round(reference, 1),
        as_of=at,
        age_seconds=0.0,
        is_stale=False,
        source="derived",
        confidence=1.0,
        # Below 80% of plan is the same line `reslot_deviation_pct` draws before
        # it starts moving people's slots — one threshold, two consumers.
        state=_band(ratio, watch=0.9, breach=0.8, inverted=True),
        detail=detail,
        note=f"Gate scans over the last {window_minutes} minutes, expressed hourly.",
        note_mr=f"गेल्या {window_minutes} मिनिटांतील स्कॅन, तासाच्या प्रमाणात.",
    )


async def _kpi_incidents(session: AsyncSession, *, at: datetime) -> Kpi:
    """Open incidents, banded on the ones nobody has answered.

    The headline number is the count of everything still open, but the *state*
    is driven by SLA breaches and criticals, not by the total. Twelve open
    lost-item reports is a busy help desk; one critical nobody has been sent to
    is the thing the strip exists to show, and a card that turns amber on volume
    would render those two the same way round.
    """
    try:
        counts = await incident_service.open_counts(session)
    except Exception as exc:
        # Same rule as everywhere else in this module: a failed read is unknown,
        # never zero. "No open incidents" and "cannot count incidents" are
        # opposite facts and must not render identically.
        logger.warning("kpi_incidents_failed", extra={"error": str(exc)})
        return _unavailable(
            "open_incidents",
            "Open incidents",
            "सुरू असलेल्या घटना",
            "count",
            "The incident board could not be read. This is not zero incidents — "
            "it is no answer. Check the board directly.",
            "घटना नोंदवही वाचता आली नाही. याचा अर्थ घटना नाहीत असा नाही — उत्तरच "
            "मिळालेले नाही. नोंदवही थेट पाहा.",
        )

    total = counts.get("total", 0)
    breached = counts.get("sla_breached", 0)
    critical = counts.get("critical", 0)

    state: KpiState = "ok"
    if breached:
        state = "breach"
    elif critical:
        state = "watch"

    note = note_mr = None
    if breached:
        note = f"{breached} incident(s) past their SLA with no unit assigned."
        note_mr = f"{breached} घटनांची मुदत संपली आहे आणि अद्याप पथक नेमलेले नाही."
    elif critical:
        note = f"{critical} critical incident(s) open. The SLA for critical is three minutes."
        note_mr = f"{critical} अतिगंभीर घटना सुरू आहेत. अतिगंभीरसाठी मुदत तीन मिनिटे आहे."

    return Kpi(
        key="open_incidents",
        label="Open incidents",
        label_mr="सुरू असलेल्या घटना",
        value=float(total),
        unit="count",
        as_of=at,
        age_seconds=0.0,
        is_stale=False,
        # Read straight from the incidents table rather than derived from a
        # reading, so it is as live as the request that asked for it.
        source="live",
        confidence=1.0,
        state=state,
        detail={
            **{status: counts.get(status, 0) for status in (str(s) for s in incident_service.OPEN_STATUSES)},
            "sla_breached": breached,
            "critical": critical,
        },
        note=note,
        note_mr=note_mr,
    )


def _kpi_cameras(cameras: list[Camera], *, at: datetime) -> Kpi:
    total = len(cameras)
    by_status: dict[str, int] = {"online": 0, "degraded": 0, "offline": 0}
    for camera in cameras:
        by_status[camera.status] = by_status.get(camera.status, 0) + 1
    calibrated = sum(1 for c in cameras if c.is_calibrated)

    detail: dict[str, object] = {
        **by_status,
        "total": total,
        "calibrated": calibrated,
        "uncalibrated": total - calibrated,
    }

    if total == 0:
        return Kpi(
            key="cameras_online",
            label="Cameras online",
            label_mr="कॅमेरे सुरू",
            value=None,
            unit="count",
            source="unavailable",
            confidence=0.0,
            state="unknown",
            detail=detail,
            note="No cameras are registered. Density is running on manual or simulated input.",
            note_mr="एकही कॅमेरा नोंदवलेला नाही. गर्दीची माहिती हाताने किंवा सिम्युलेशनने येत आहे.",
        )

    online = by_status["online"]
    offline_share = (total - online) / total

    note = note_mr = None
    if calibrated < total:
        # An uncalibrated camera still reports "online" while contributing a
        # density figure derived from no measured ground plane.
        note = f"{total - calibrated} camera(s) online but uncalibrated — their density figures are estimates."
        note_mr = f"{total - calibrated} कॅमेरे सुरू आहेत पण कॅलिब्रेट केलेले नाहीत — त्यांचे आकडे अंदाजे आहेत."

    return Kpi(
        key="cameras_online",
        label="Cameras online",
        label_mr="कॅमेरे सुरू",
        value=float(online),
        unit="count",
        target=float(total),
        as_of=at,
        age_seconds=0.0,
        is_stale=False,
        source="live",
        confidence=1.0,
        state=_band(offline_share, watch=0.001, breach=0.2),
        detail=detail,
        note=note,
        note_mr=note_mr,
    )


async def _kpi_breaches(session: AsyncSession, *, at: datetime) -> Kpi:
    """Breach events still waiting for a human (Section 4/M5).

    This card counts *unreviewed* events, not breaches — the distinction is the
    whole point of the module. An event is a detection; it becomes a finding
    only when a Security Officer marks it verified. A KPI labelled "breaches"
    that counted raw detections would put an AI output on the wall of a control
    room as though it were a fact.

    The chain check rides along in the detail. A pending count read off a ledger
    that does not verify is a number nobody should act on, and an operator
    should not have to visit a second screen to find that out.
    """
    try:
        pending = await breach_service.pending_count(session)
        report = await breach_service.verify_chain(session)
    except Exception as exc:
        logger.warning("kpi_breaches_failed", extra={"error": str(exc)})
        return _unavailable(
            "breaches_pending_review",
            "Breaches pending review",
            "पुनरावलोकन बाकी उल्लंघने",
            "count",
            "The breach ledger could not be read. This is not zero breaches — it is no answer.",
            "उल्लंघन नोंदवही वाचता आली नाही. याचा अर्थ उल्लंघने नाहीत असा नाही — उत्तरच मिळालेले नाही.",
        )

    note = note_mr = None
    state: KpiState = "ok"
    if not report.intact:
        # A broken chain outranks any backlog. The number of pending reviews is
        # irrelevant if the ledger they came from cannot be trusted.
        state = "breach"
        note = (
            f"The evidence chain does not verify ({len(report.breaks)} break(s)). "
            "Do not act on this ledger until it has been investigated."
        )
        note_mr = (
            f"पुराव्याची साखळी तपासणीत टिकत नाही ({len(report.breaks)} ठिकाणी). "
            "तपास होईपर्यंत या नोंदवहीवर कारवाई करू नका."
        )
    elif pending >= 20:
        state = "breach"
        note = "The review backlog is large. Unreviewed events are detections, not findings."
        note_mr = "पुनरावलोकनाचा अनुशेष मोठा आहे. न तपासलेल्या नोंदी म्हणजे निष्कर्ष नव्हेत."
    elif pending > 0:
        state = "watch"

    return Kpi(
        key="breaches_pending_review",
        label="Breaches pending review",
        label_mr="पुनरावलोकन बाकी उल्लंघने",
        value=float(pending),
        unit="count",
        as_of=at,
        age_seconds=0.0,
        is_stale=False,
        source="live",
        confidence=1.0,
        state=state,
        detail={
            "pending": pending,
            "chain_intact": report.intact,
            "chain_breaks": len(report.breaks),
            "events_in_ledger": report.events_checked,
            "chain_head": report.head_hash,
        },
        note=note,
        note_mr=note_mr,
    )


async def kpi_strip(session: AsyncSession, *, at: datetime | None = None) -> KpiStrip:
    """The six numbers across the top of the console."""
    moment = at or now_utc()

    zones = await crowd_service.load_zones(session)
    snapshots = await crowd_service.latest(session)
    fresh = _partition(zones, snapshots)

    cameras = list((await session.execute(select(Camera))).scalars())

    window_minutes = 30
    observed_per_hour: float | None = None
    planned_per_hour: float | None = None
    try:
        window = await pass_service.measure_throughput(
            session, at=moment, window_minutes=window_minutes
        )
        if window.planned > 0 or window.actual > 0:
            observed_per_hour = 60.0 * window.actual / window.minutes
            planned_per_hour = 60.0 * window.planned / window.minutes
    except Exception as exc:
        # A failed throughput read must not take the whole strip down with it;
        # the card reports unavailable and the other five still render.
        logger.warning("kpi_throughput_failed", extra={"error": str(exc)})

    target = await config_service.get_int(session, "temple_throughput_per_hour")

    kpis = [
        _kpi_pilgrims(fresh, zones, at=moment),
        _kpi_wait(fresh, zones, observed_per_hour=observed_per_hour, at=moment),
        _kpi_throughput(
            observed_per_hour=observed_per_hour,
            planned_per_hour=planned_per_hour,
            target_per_hour=target,
            window_minutes=window_minutes,
            at=moment,
        ),
        await _kpi_incidents(session, at=moment),
        await _kpi_breaches(session, at=moment),
        _kpi_cameras(cameras, at=moment),
    ]

    return KpiStrip(
        kpis=kpis,
        generated_at=moment,
        stale_count=sum(1 for k in kpis if k.is_stale),
        unknown_count=sum(1 for k in kpis if k.value is None),
    )


# ---------------------------------------------------------------------------
# what changed in the last 15 minutes
# ---------------------------------------------------------------------------
_LEVEL_ORDER = {
    DensityLevel.SAFE: 0,
    DensityLevel.MODERATE: 1,
    DensityLevel.HIGH: 2,
    DensityLevel.CRITICAL: 3,
}

_LEVEL_MR = {
    DensityLevel.SAFE: "सुरक्षित",
    DensityLevel.MODERATE: "मध्यम",
    DensityLevel.HIGH: "जास्त",
    DensityLevel.CRITICAL: "अतिगंभीर",
}

#: A transition *to* these levels is worth waking someone for.
_LEVEL_SEVERITY: dict[DensityLevel, str] = {
    DensityLevel.SAFE: "info",
    DensityLevel.MODERATE: "info",
    DensityLevel.HIGH: "warning",
    DensityLevel.CRITICAL: "critical",
}

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


async def _zone_level_changes(
    session: AsyncSession,
    zones: dict[uuid.UUID, Zone],
    *,
    since: datetime,
    until: datetime,
) -> list[ChangeItem]:
    """Level transitions per zone, from the 1-minute rollups.

    The read starts ten minutes before the window so the first in-window bucket
    has something to be a transition *from*.  Without that baseline every zone
    appears to "change" the moment the digest opens, which trains operators to
    ignore the strip — the exact opposite of what it is for.
    """
    baseline_start = since - timedelta(minutes=10)
    try:
        series = await crowd_service.series_all(session, since=baseline_start, until=until)
    except Exception as exc:
        logger.warning("digest_series_failed", extra={"error": str(exc)})
        return []

    items: list[ChangeItem] = []
    for zone_id, points in series.items():
        zone = zones.get(zone_id)
        if zone is None:
            continue
        previous: DensityLevel | None = None
        for point in points:
            level = point.peak_level
            if previous is not None and level != previous and point.bucket >= since:
                rising = _LEVEL_ORDER[level] > _LEVEL_ORDER[previous]
                verb, verb_mr = ("rose to", "पर्यंत वाढली") if rising else ("eased to", "पर्यंत कमी झाली")
                reading = f"{point.peak_density:.1f}"
                items.append(
                    ChangeItem(
                        at=point.bucket,
                        kind="zone_level",
                        severity=_LEVEL_SEVERITY[level] if rising else "info",  # type: ignore[arg-type]
                        summary=f"{zone.code} {zone.name} {verb} {level} ({reading} p/m²)",
                        summary_mr=(
                            f"{zone.code} {zone.name_mr} {_LEVEL_MR[level]} {verb_mr} ({reading} प्र/मी²)"
                        ),
                        zone_code=zone.code,
                        ref_type="zone",
                        ref_id=zone.id,
                        from_level=previous,
                        to_level=level,
                    )
                )
            previous = level
    return items


def _alert_change_items(alert: Alert, zone: Zone | None, *, since: datetime, until: datetime) -> list[ChangeItem]:
    """Every lifecycle step this alert took inside the window.

    One alert can contribute several lines — raised at 14:02, escalated at
    14:03, acknowledged at 14:05 is three things that happened, and collapsing
    them to "one alert" loses the fact that it sat unacknowledged for three
    minutes.
    """
    zone_code = zone.code if zone else None
    where = f" in {zone.code}" if zone else ""
    where_mr = f" ({zone.name_mr})" if zone else ""

    # A camera-offline alert *is* the persisted record of a camera changing
    # status — cameras keep only their current state, no history. Classifying
    # it as `camera_status` rather than `alert_raised` keeps the digest from
    # reporting the same fact under two kinds.
    is_camera = alert.type == "camera_offline"

    def item(at: datetime, kind: ChangeKind, severity: str, summary: str, summary_mr: str) -> ChangeItem:
        return ChangeItem(
            at=at,
            kind=kind,
            severity=severity,  # type: ignore[arg-type]
            summary=summary,
            summary_mr=summary_mr,
            zone_code=zone_code,
            ref_type="alert",
            ref_id=alert.id,
        )

    out: list[ChangeItem] = []

    def in_window(moment: datetime | None) -> bool:
        return moment is not None and since <= moment <= until

    if in_window(alert.created_at):
        if is_camera:
            out.append(
                item(
                    alert.created_at,
                    "camera_status",
                    alert.severity,
                    f"Camera went offline{where}",
                    f"कॅमेरा बंद पडला{where_mr}",
                )
            )
        else:
            out.append(
                item(
                    alert.created_at,
                    "alert_raised",
                    alert.severity,
                    f"{alert.severity.upper()} {alert.type}{where} — {alert.trigger_metric} {alert.trigger_value:.2f}",
                    f"{alert.type}{where_mr} — {alert.trigger_metric} {alert.trigger_value:.2f}",
                )
            )

    if in_window(alert.escalated_at):
        out.append(
            item(
                alert.escalated_at,  # type: ignore[arg-type]
                "alert_escalated",
                "critical",
                f"Escalated to level {alert.escalation_level}: {alert.type}{where}",
                f"पातळी {alert.escalation_level} पर्यंत वाढवले: {alert.type}{where_mr}",
            )
        )

    if in_window(alert.acknowledged_at):
        out.append(
            item(
                alert.acknowledged_at,  # type: ignore[arg-type]
                "alert_acknowledged",
                "info",
                f"Acknowledged: {alert.type}{where}",
                f"स्वीकारले: {alert.type}{where_mr}",
            )
        )

    if in_window(alert.resolved_at):
        if is_camera:
            out.append(
                item(
                    alert.resolved_at,  # type: ignore[arg-type]
                    "camera_status",
                    "info",
                    f"Camera back online{where}",
                    f"कॅमेरा पुन्हा सुरू{where_mr}",
                )
            )
        else:
            out.append(
                item(
                    alert.resolved_at,  # type: ignore[arg-type]
                    "alert_resolved",
                    "info",
                    f"Resolved: {alert.type}{where}",
                    f"निकाली काढले: {alert.type}{where_mr}",
                )
            )
    return out


async def change_digest(
    session: AsyncSession,
    *,
    minutes: int = DIGEST_MINUTES,
    limit: int = DIGEST_LIMIT,
    at: datetime | None = None,
) -> ChangeDigest:
    """"What changed while I was away" (Section 4/M3, last rule).

    Ordered worst-first then newest-first, matching the alert feed — an
    operator's eye should land in the same place on both rails.
    """
    until = at or now_utc()
    since = until - timedelta(minutes=minutes)

    zones = await crowd_service.load_zones(session)
    items = await _zone_level_changes(session, zones, since=since, until=until)

    # Any alert that *touched* the window: raised inside it, or raised earlier
    # and acted on inside it.
    rows = await session.execute(
        select(Alert).where(
            (Alert.created_at >= since)
            | (Alert.acknowledged_at >= since)
            | (Alert.escalated_at >= since)
            | (Alert.resolved_at >= since)
        )
    )
    alerts = list(rows.scalars())

    zone_by_id = dict(zones)
    missing = {a.zone_id for a in alerts if a.zone_id and a.zone_id not in zone_by_id}
    if missing:
        # Alerts can outlive a zone being deactivated; the digest still names it.
        extra = await session.execute(select(Zone).where(Zone.id.in_(missing)))
        zone_by_id.update({z.id: z for z in extra.scalars()})

    for alert in alerts:
        items.extend(
            _alert_change_items(
                alert,
                zone_by_id.get(alert.zone_id) if alert.zone_id else None,
                since=since,
                until=until,
            )
        )

    items.sort(key=lambda i: (_SEVERITY_RANK.get(i.severity, 3), -i.at.timestamp()))
    truncated = len(items) > limit

    return ChangeDigest(
        since=since,
        until=until,
        items=items[:limit],
        truncated=truncated,
        generated_at=until,
    )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
_REPLAY_NOTE = (
    "Replay is built from one-minute rollups. Values are the peak within each "
    "minute, and minutes with no reading are shown as unknown rather than held "
    "at the previous colour."
)
_REPLAY_NOTE_MR = (
    "रीप्ले एक-मिनिटाच्या सरासरीवर आधारित आहे. प्रत्येक मिनिटातील उच्चांक दाखवला जातो, "
    "आणि माहिती नसलेली मिनिटे मागचा रंग न ठेवता 'माहिती नाही' अशी दाखवली जातात."
)


async def replay_window(
    session: AsyncSession,
    *,
    minutes: int = 60,
    until: datetime | None = None,
    zone_codes: list[str] | None = None,
) -> ReplayWindow:
    """Frames for the time-scrubber.

    Two things this deliberately does not do.  It does not carry a zone's last
    known value forward into a minute that has no reading — a replay that holds
    a green zone green through a pipeline outage is a replay that lies about the
    one interval anybody will ask about afterwards.  And it does not
    interpolate between buckets to produce smoother playback; the smoothing
    belongs in the client's colour transition, not in the data.
    """
    end = until or now_utc()
    minutes = max(1, min(minutes, REPLAY_MAX_MINUTES))
    start = end - timedelta(minutes=minutes)

    zones = await crowd_service.load_zones(session)
    if zone_codes:
        wanted = {c.upper() for c in zone_codes}
        zones = {zid: z for zid, z in zones.items() if z.code in wanted}

    series = await crowd_service.series_all(session, since=start, until=end)

    # bucket -> zone_id -> state.  Buckets come from the data rather than from a
    # generated range, so a gap in the pipeline is a gap in the scrubber.
    by_bucket: dict[datetime, dict[uuid.UUID, ReplayZoneState]] = {}
    for zone_id, points in series.items():
        zone = zones.get(zone_id)
        if zone is None:
            continue
        for point in points:
            if point.sample_count <= 0:
                continue
            by_bucket.setdefault(point.bucket, {})[zone_id] = ReplayZoneState(
                zone_id=zone_id,
                zone_code=zone.code,
                density=point.peak_density,
                level=classify_density(point.peak_density),
                person_count=int(round(point.avg_person_count)),
                stagnation_index=point.peak_stagnation,
                counterflow_ratio=point.peak_counterflow,
                confidence=point.avg_confidence,
                sample_count=point.sample_count,
            )

    alert_counts = await _alerts_per_minute(session, since=start, until=end)
    all_codes = sorted(z.code for z in zones.values())

    frames: list[ReplayFrame] = []
    for bucket in sorted(by_bucket):
        states = by_bucket[bucket]
        present = {s.zone_code for s in states.values()}
        open_count, critical_count = alert_counts.get(bucket, (0, 0))
        frames.append(
            ReplayFrame(
                at=bucket,
                zones=sorted(states.values(), key=lambda s: s.zone_code),
                unknown_zones=[c for c in all_codes if c not in present],
                open_alerts=open_count,
                critical_alerts=critical_count,
            )
        )

    return ReplayWindow(
        since=start,
        until=end,
        step_seconds=REPLAY_STEP_SECONDS,
        frames=frames,
        zone_codes=all_codes,
        generated_at=now_utc(),
        note=_REPLAY_NOTE,
        note_mr=_REPLAY_NOTE_MR,
    )


async def _alerts_per_minute(
    session: AsyncSession, *, since: datetime, until: datetime
) -> dict[datetime, tuple[int, int]]:
    """How many alerts were open at each minute of the window.

    "Open at time T" means raised at or before T and not yet resolved at T — so
    an alert that ran through the whole window is counted in every frame, which
    is what an operator scrubbing back expects to see.  Alerts are few enough
    per window that walking them in Python beats a generate_series join.
    """
    rows = await session.execute(
        select(Alert.created_at, Alert.resolved_at, Alert.severity).where(
            Alert.created_at <= until,
            (Alert.resolved_at.is_(None)) | (Alert.resolved_at >= since),
        )
    )
    alerts = list(rows.all())
    if not alerts:
        return {}

    counts: dict[datetime, tuple[int, int]] = {}
    cursor = since.replace(second=0, microsecond=0)
    while cursor <= until:
        open_count = 0
        critical_count = 0
        for created_at, resolved_at, severity in alerts:
            if created_at <= cursor and (resolved_at is None or resolved_at > cursor):
                open_count += 1
                if severity == "critical":
                    critical_count += 1
        if open_count:
            counts[cursor] = (open_count, critical_count)
        cursor += timedelta(seconds=REPLAY_STEP_SECONDS)
    return counts


# ---------------------------------------------------------------------------
# escalation clock, read-only
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    """The two numbers the console's alert cards count against.

    Served rather than hardcoded in the client because they are operator-tunable
    (`system_config`), and a console counting to 60 while the server escalates
    at 90 would show an alert turning red before anything happened.
    """

    escalate_seconds: int
    page_seconds: int


async def escalation_policy(session: AsyncSession) -> EscalationPolicy:
    return EscalationPolicy(
        escalate_seconds=await config_service.get_int(session, "alert_escalate_seconds"),
        page_seconds=await config_service.get_int(session, "alert_page_seconds"),
    )


async def live_alert_counts(session: AsyncSession) -> dict[str, int]:
    """Open/acknowledged/escalated counts, for the feed header."""
    rows = await session.execute(
        select(Alert.status, func.count())
        .where(Alert.status.in_([str(s) for s in alert_service.LIVE_STATUSES]))
        .group_by(Alert.status)
    )
    return {status: int(count) for status, count in rows.all()}
