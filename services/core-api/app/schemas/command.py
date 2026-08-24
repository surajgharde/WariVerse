"""Command-centre schemas (Section 4/M3).

Three shapes, one audience: the single-screen operations console.

* `KpiStrip` — the six numbers across the top.
* `ChangeDigest` — "what changed in the last 15 minutes", for the operator who
  just walked back in.
* `ReplayWindow` — the time-scrubber's frames.

A note on where the Marathi lives.  Operational text — what a number means,
why it is unknown, what changed — is generated here, because it depends on
server-side state and both consoles must say exactly the same thing.  UI chrome
(tab names, button labels, column headings) is the client's i18n bundle.  The
split is: **if the string describes a fact, the server writes it.**

The other rule this file exists to enforce is Section 4/M3's hardest one: an
operator must never act on a number they believe is live but is not.  So
`value` is nullable and `None` means *not measured* — never zero.  Zero
pilgrims and no camera reporting are opposite facts, and a strip that renders
them identically is the bug that gets somebody hurt.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.crowd import DensityLevel
from app.schemas.common import ApiModel

#: How a KPI should read at a glance.  Computed server-side so the thresholds
#: that decide "this is fine" live in one place with the alert rules, and not
#: in a stylesheet where nobody reviews them.
KpiState = Literal["ok", "watch", "breach", "unknown"]

KpiUnit = Literal["persons", "minutes", "per_hour", "count", "ratio"]


class Kpi(ApiModel):
    """One card in the top strip, with its provenance attached."""

    key: str
    label: str
    label_mr: str

    #: `None` means "we are not measuring this", and the console renders it as
    #: a dash with an explanation — never as 0.
    value: float | None
    unit: KpiUnit
    #: The planned figure, where one exists (throughput). Lets the card show
    #: actual-against-target without the client knowing the config.
    target: float | None = None

    as_of: datetime | None = None
    age_seconds: float | None = None
    is_stale: bool = False
    #: live | video | sim | derived | config | unavailable
    source: str = "derived"
    confidence: float = 1.0

    state: KpiState = "unknown"
    #: Per-KPI breakdown — zone coverage, camera counts, scan totals. The card
    #: shows this on expand; the operator asking "where does that come from"
    #: should not have to ask twice.
    detail: dict[str, Any] = Field(default_factory=dict)

    note: str | None = None
    note_mr: str | None = None


class KpiStrip(ApiModel):
    kpis: list[Kpi]
    generated_at: datetime
    #: Surfaced so the console can badge the whole strip when several numbers
    #: have gone cold at once — that pattern means a pipeline died, not a quiet
    #: temple, and it deserves to be visible without reading six cards.
    stale_count: int = 0
    unknown_count: int = 0


# ---------------------------------------------------------------------------
# what changed in the last 15 minutes
# ---------------------------------------------------------------------------
ChangeKind = Literal[
    "zone_level",
    "alert_raised",
    "alert_acknowledged",
    "alert_escalated",
    "alert_resolved",
    "camera_status",
]


class ChangeItem(ApiModel):
    """One line in the catch-up strip.  Newest first, worst first."""

    at: datetime
    kind: ChangeKind
    severity: Literal["info", "warning", "critical"]
    summary: str
    summary_mr: str
    zone_code: str | None = None
    #: The alert, camera or zone this line refers to, so clicking it can focus
    #: the map or open the alert rather than leaving the operator to search.
    ref_type: str | None = None
    ref_id: uuid.UUID | None = None
    #: Present on `zone_level` — what it moved from and to.
    from_level: DensityLevel | None = None
    to_level: DensityLevel | None = None


class ChangeDigest(ApiModel):
    since: datetime
    until: datetime
    items: list[ChangeItem]
    #: True when the window held more than `limit` changes. A digest that
    #: silently drops half a surge is worse than one that admits it.
    truncated: bool = False
    generated_at: datetime


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
class ReplayZoneState(ApiModel):
    """One zone at one instant.  Deliberately thinner than `ZoneStatusDetail` —
    a sixty-frame replay carrying every field is a megabyte of JSON nobody
    reads."""

    zone_id: uuid.UUID
    zone_code: str
    density: float
    level: DensityLevel
    person_count: int
    stagnation_index: float
    counterflow_ratio: float
    confidence: float
    #: How many 10-second readings landed in this minute. 0 buckets are absent
    #: from the frame entirely rather than present as zeros — see the module
    #: docstring.
    sample_count: int


class ReplayFrame(ApiModel):
    at: datetime
    zones: list[ReplayZoneState]
    #: Zones with no reading in this minute, by code. The scrubber greys them
    #: rather than holding the previous colour, because holding the last known
    #: colour through a pipeline outage is how a replay lies.
    unknown_zones: list[str] = Field(default_factory=list)
    open_alerts: int = 0
    critical_alerts: int = 0


class ReplayWindow(ApiModel):
    since: datetime
    until: datetime
    step_seconds: int
    frames: list[ReplayFrame]
    #: Every zone in the window, for a stable map legend and stable colours —
    #: the set must not change from frame to frame as zones drop in and out.
    zone_codes: list[str]
    generated_at: datetime
    note: str
    note_mr: str


# ---------------------------------------------------------------------------
# console bootstrap
# ---------------------------------------------------------------------------
class ConsoleConfig(ApiModel):
    """The handful of server-side numbers the console must not guess at.

    Every one of these is operator-tunable in `system_config` or environment
    config. A console that hardcodes them drifts from the server the first time
    an administrator changes one — and drift here is visible as an alert card
    that turns red before the server escalates, which is precisely the kind of
    small lie that costs an operator their trust in the screen.
    """

    #: Seconds an unacknowledged CRITICAL waits before it escalates visually.
    alert_escalate_seconds: int
    #: Seconds before it pages the next role in the chain.
    alert_page_seconds: int
    #: A reading older than this renders greyed with a stale badge.
    stale_reading_seconds: int
    #: How often the AI engine reports, so the console can size its own polling
    #: instead of hammering an endpoint that changes every ten seconds.
    crowd_window_seconds: int
    #: live | video | sim — the console shows this, because an operator must
    #: always know whether they are watching the temple or a simulation.
    crowd_source: str
    #: Density band boundaries in people/m², so the map legend and the server
    #: agree on where green becomes amber.
    density_thresholds: dict[str, float]
    #: Open/acknowledged/escalated counts for the feed header.
    live_alert_counts: dict[str, int]
    server_time: datetime
