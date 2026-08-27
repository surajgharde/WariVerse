"""Domain metrics (Section 11 OBSERVABILITY, Phase 10).

`middleware.py` already exports per-endpoint request counts and latency.  That
answers "is the API healthy".  Section 11 asks for something else as well —
"Prometheus metrics per endpoint **and per zone pipeline**" — which answers a
different question: *is the system still seeing the Wari?*

Those two can disagree, and the gap between them is where an outage hides.  An
API serving 200 OK on every route while forty cameras have gone dark is a green
dashboard over a blind control room.  Everything in this module exists to make
that state visible.

**How these are updated: at the point the data already flows, never by
querying.**  A `/metrics` scrape must not run a database query — Prometheus
polls every 15 seconds, and a collector that fans out to Postgres turns
monitoring into load, which is precisely the wrong behaviour on the day the
system is under strain.  So the ingest path sets the zone gauges it was already
holding, the camera watchdog sets the coverage gauges it already computed, and
the Palkhi sweep sets the Dindi gauges it already walked.  Every value here is a
by-product of work being done anyway.

**Why gauges go stale on purpose.**  Nothing resets these on a timer.  If the
AI engine dies, `wariverse_zone_density` keeps its last value and
`wariverse_zone_reading_age_seconds` climbs without limit — and the alert rule
in `infra/prometheus/alerts.yml` fires on the *age*, not on the density. A
metric that reset to zero when its feed died would render an unmeasured zone as
an empty one, which is the same lie the Redis TTL in `crowd_service` exists to
prevent.

Cardinality: labels are zone codes and severities — tens of series, not
thousands. No pass id, no Dindi id, no user id ever becomes a label.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# the zone pipeline
# ---------------------------------------------------------------------------
ZONE_DENSITY = Gauge(
    "wariverse_zone_density_persons_per_m2",
    "Latest measured crowd density for a zone",
    ["zone"],
)
ZONE_PERSON_COUNT = Gauge(
    "wariverse_zone_person_count",
    "Latest measured person count for a zone",
    ["zone"],
)
ZONE_STAGNATION = Gauge(
    "wariverse_zone_stagnation_index",
    "Latest stagnation index for a zone. Rises before density in a crush precursor.",
    ["zone"],
)
ZONE_COUNTERFLOW = Gauge(
    "wariverse_zone_counterflow_ratio",
    "Latest counter-flow ratio for a zone",
    ["zone"],
)
ZONE_CONFIDENCE = Gauge(
    "wariverse_zone_reading_confidence",
    "Confidence attached to the zone's latest reading. Below 1.0 the figure is an estimate.",
    ["zone"],
)
#: The most important series in this file. It is what the staleness alert fires
#: on, and it is the one number that distinguishes "this zone is quiet" from
#: "we stopped being able to see this zone".
ZONE_READING_AGE = Gauge(
    "wariverse_zone_reading_age_seconds",
    "Seconds since this zone's last accepted reading. Climbs without limit when the feed dies.",
    ["zone"],
)

READINGS_INGESTED = Counter(
    "wariverse_readings_ingested_total",
    "Crowd readings accepted from the AI engine",
    ["source"],
)
READINGS_REJECTED = Counter(
    "wariverse_readings_rejected_total",
    "Crowd readings refused at ingest, by reason",
    ["reason"],
)

# ---------------------------------------------------------------------------
# camera coverage
# ---------------------------------------------------------------------------
CAMERAS_ONLINE = Gauge("wariverse_cameras_online", "Cameras that have sent a heartbeat recently")
CAMERAS_TOTAL = Gauge("wariverse_cameras_total", "Cameras configured")
CAMERAS_CALIBRATED = Gauge(
    "wariverse_cameras_calibrated",
    "Cameras with a homography. An uncalibrated camera's density figure is fiction.",
)

# ---------------------------------------------------------------------------
# alerts and incidents
# ---------------------------------------------------------------------------
ALERTS_RAISED = Counter(
    "wariverse_alerts_raised_total",
    "Alerts raised, by type and severity",
    ["type", "severity"],
)
ALERTS_OPEN = Gauge(
    "wariverse_alerts_open",
    "Alerts currently demanding an operator's attention, by severity",
    ["severity"],
)
#: Section 11 wants alerting on things nobody answered. This is the counter a
#: post-Wari review reads to answer "how often did the control room not respond
#: in time", which is a different and harder question than "how many incidents".
INCIDENT_SLA_BREACHES = Counter(
    "wariverse_incident_sla_breaches_total",
    "Incidents that passed their SLA with no responder assigned",
    ["severity"],
)

# ---------------------------------------------------------------------------
# passes
# ---------------------------------------------------------------------------
PASSES_ISSUED = Counter("wariverse_passes_issued_total", "Darshan passes issued")
PASSES_SCANNED = Counter(
    "wariverse_pass_scans_total", "Pass scans at a checkpoint, by outcome", ["outcome"]
)
PASSES_RESLOTTED = Counter(
    "wariverse_passes_reslotted_total", "Passes moved by the dynamic reslotting job"
)

# ---------------------------------------------------------------------------
# palkhi (Phase 9)
# ---------------------------------------------------------------------------
DINDIS_REPORTING = Gauge("wariverse_dindis_reporting", "Dindis whose tracking phone is still reporting")
#: Paired with the one above on purpose. Fourteen dots on a map means nothing
#: without "and six more groups are walking that we cannot see".
DINDIS_SILENT = Gauge("wariverse_dindis_silent", "Walking Dindis whose phone has gone quiet")
DINDIS_DEVIATING = Gauge(
    "wariverse_dindis_deviating", "Dindis outside their schedule threshold for the next halt town"
)

# ---------------------------------------------------------------------------
# assistant (Phase 9)
# ---------------------------------------------------------------------------
#: Split by outcome because the outcomes mean opposite things about the system.
#: A rising `no_data` rate is an outage; a rising `refused` rate is a product
#: telling users the wrong thing about what the assistant is for; a rising
#: `degraded` rate means the model has been unreachable and nobody noticed.
ASSISTANT_TURNS = Counter(
    "wariverse_assistant_turns_total", "Assistant turns, by outcome", ["outcome"]
)


# ---------------------------------------------------------------------------
# observers — called from the paths that already hold the data
# ---------------------------------------------------------------------------
def observe_zone(
    *,
    zone_code: str,
    density: float,
    person_count: int,
    stagnation_index: float,
    counterflow_ratio: float,
    confidence: float,
    age_seconds: float,
) -> None:
    """One accepted reading.  Called from the ingest path, which is already
    holding every one of these values."""
    ZONE_DENSITY.labels(zone_code).set(density)
    ZONE_PERSON_COUNT.labels(zone_code).set(person_count)
    ZONE_STAGNATION.labels(zone_code).set(stagnation_index)
    ZONE_COUNTERFLOW.labels(zone_code).set(counterflow_ratio)
    ZONE_CONFIDENCE.labels(zone_code).set(confidence)
    ZONE_READING_AGE.labels(zone_code).set(max(0.0, age_seconds))


def observe_camera_coverage(*, online: int, total: int, calibrated: int) -> None:
    CAMERAS_ONLINE.set(online)
    CAMERAS_TOTAL.set(total)
    CAMERAS_CALIBRATED.set(calibrated)


def observe_alert_raised(alert_type: str, severity: str) -> None:
    ALERTS_RAISED.labels(alert_type, severity).inc()


def observe_open_alerts(counts: dict[str, int]) -> None:
    """Set the open-alert gauge for every severity, including the empty ones.

    Severities absent from `counts` are set to 0 rather than left alone — a
    gauge that keeps yesterday's CRITICAL count because today has none is worse
    than no gauge.
    """
    for severity in ("info", "warning", "critical"):
        ALERTS_OPEN.labels(severity).set(counts.get(severity, 0))


def observe_dindis(*, reporting: int, silent: int, deviating: int) -> None:
    DINDIS_REPORTING.set(reporting)
    DINDIS_SILENT.set(silent)
    DINDIS_DEVIATING.set(deviating)


def observe_assistant_turn(outcome: str) -> None:
    ASSISTANT_TURNS.labels(outcome).inc()
