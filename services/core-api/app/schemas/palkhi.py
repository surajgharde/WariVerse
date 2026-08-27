"""Request and response shapes for Palkhi tracking (Section 4/M8, Phase 9).

Two conventions run through this file and are worth stating once.

**Provenance travels with the number.**  An ETA on this board can come from
ninety minutes of route-projected walking or from two dots and a straight line,
and those are different claims about when five hundred people arrive.  So every
estimate carries `pace_method`, `pace_samples` and `is_estimate` — the same
contract `Observation` gives density readings elsewhere in this API.

**Absent is not zero.**  A Dindi whose phone is silent has `eta: null`, not a
projected time; a town with nobody scheduled has `readiness: "unknown"`, not
`"ready"`.  Rendering either of those as a confident value is how a halt town
staffs a kitchen for a group that is four hours away.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import ApiModel


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
class DindiCreate(ApiModel):
    code: str = Field(min_length=2, max_length=20, description="Short registration code, e.g. DND-014")
    name: str = Field(min_length=2, max_length=160)
    name_mr: str = Field(min_length=1, max_length=200)
    leader_name: str = Field(min_length=2, max_length=120)
    #: Raw number.  Hashed onto the Dindi row and encrypted into
    #: `contact_secrets` with a season TTL — it is never stored in the clear on
    #: the entity, and never returned in a list response.
    leader_phone: str = Field(min_length=10, max_length=20)
    expected_count: int = Field(gt=0, le=20_000)
    route_id: uuid.UUID | None = None
    tracking_device_id: str | None = Field(default=None, max_length=80)

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class DindiUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    name_mr: str | None = Field(default=None, min_length=1, max_length=200)
    leader_name: str | None = Field(default=None, min_length=2, max_length=120)
    leader_phone: str | None = Field(default=None, min_length=10, max_length=20)
    expected_count: int | None = Field(default=None, gt=0, le=20_000)
    route_id: uuid.UUID | None = None
    is_active: bool | None = None
    status: str | None = Field(default=None, max_length=16)
    #: Moving the registration to a new phone.  Its own field rather than a
    #: silent side effect of a ping, because Section 4/M8 designates *one*
    #: device per Dindi and changing which one is a decision, not an accident.
    tracking_device_id: str | None = Field(default=None, max_length=80)


class ScheduleStopIn(ApiModel):
    halt_town_id: uuid.UUID
    planned_arrival: datetime
    planned_departure: datetime | None = None
    expected_count: int | None = Field(default=None, gt=0, le=20_000)


class ScheduleIn(ApiModel):
    """The whole schedule, replaced as a unit.

    Not a per-stop PATCH: a halt schedule is a sequence, and editing one arrival
    time in isolation is how you end up with day nine before day eight.  The
    order of this list *is* the walking order.
    """

    stops: list[ScheduleStopIn] = Field(min_length=1, max_length=60)


class ScheduleStopOut(ApiModel):
    halt_town_id: uuid.UUID
    halt_town: str
    halt_town_mr: str
    sequence: int
    planned_arrival: datetime
    planned_departure: datetime | None = None
    actual_arrival: datetime | None = None
    actual_departure: datetime | None = None
    expected_count: int
    #: Set once the group has actually arrived: how far off the plan it was, in
    #: minutes, positive for late.  This is the column next year's schedule gets
    #: built from.
    arrival_deviation_minutes: float | None = None


# ---------------------------------------------------------------------------
# position reporting
# ---------------------------------------------------------------------------
class PingIn(ApiModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    battery: int | None = Field(default=None, ge=0, le=100)
    speed_kmph: float | None = Field(default=None, ge=0, le=200)
    accuracy_m: float | None = Field(default=None, ge=0)
    #: The device's own timestamp.  Accepted so a phone that was offline can
    #: flush its queue with the times things actually happened; an old ping is
    #: stored as history and never overwrites a newer known position.
    at: datetime | None = None


class PingAck(ApiModel):
    accepted: bool = True
    recorded_at: datetime
    status: str
    #: How long the device should wait before reporting again.  The server sets
    #: this, not the app: the phone knows its battery, but only the server knows
    #: the group is halted for the night with six days of walking left to power.
    next_ping_seconds: int
    route_fraction: float | None = None
    off_route_m: float | None = None
    arrived_at: str | None = None
    departed_from: str | None = None
    #: One line the volunteer can read on a phone in daylight, in Marathi.
    summary_mr: str
    summary: str


# ---------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------
class PaceOut(ApiModel):
    kmph: float
    samples: int
    span_minutes: float
    km_covered: float
    #: route | straight | none.  A crow-flies pace under-states a winding road
    #: by 10-20% on the Alandi route, and an operator moving a town's water
    #: tankers is entitled to know which one they are looking at.
    method: str
    is_usable: bool


class DindiOut(ApiModel):
    id: uuid.UUID
    code: str
    name: str
    name_mr: str
    leader_name: str
    #: No phone number, in any form. Section 8's PII rule means the Dindi row
    #: holds only an HMAC, and a masked number would be worse than useless here
    #: anyway: a roster of four hundred leaders' partial numbers is still a
    #: roster. The real number is behind `GET /dindis/{id}/leader-contact`,
    #: which is audited on every read.
    expected_count: int
    route_id: uuid.UUID | None = None
    route_name: str | None = None
    status: str
    is_active: bool

    #: [lon, lat] of the last report, GeoJSON order as everywhere else here.
    position: tuple[float, float] | None = None
    last_ping_at: datetime | None = None
    seconds_since_ping: int | None = None
    battery: int | None = None
    #: True when the phone has been quiet past `dindi_signal_lost_minutes`. The
    #: position above is then a historical fact, not a current one, and the app
    #: is required to render it with its age.
    is_signal_lost: bool = False
    off_route_m: float | None = None

    km_walked: float | None = None
    route_fraction: float | None = None
    pace: PaceOut | None = None

    next_town: str | None = None
    next_town_mr: str | None = None
    next_town_id: uuid.UUID | None = None
    km_remaining: float | None = None
    planned_arrival: datetime | None = None
    #: Null whenever the pace cannot carry the arithmetic — a stopped group, a
    #: silent phone, too few reports. Never a default walking speed dressed up
    #: as a measurement.
    eta: datetime | None = None
    deviation_minutes: float | None = None
    is_deviating: bool = False


class DindiDetail(DindiOut):
    schedule: list[ScheduleStopOut] = Field(default_factory=list)
    tracking_device_registered: bool = False


class DindiList(ApiModel):
    items: list[DindiOut]
    generated_at: datetime
    #: How many are reporting versus how many should be. The board's honesty
    #: figure: fourteen dots on a map means nothing without "and six more groups
    #: are walking that we cannot see".
    reporting: int
    silent: int
    notice: str | None = None
    notice_mr: str | None = None


class LeaderContact(ApiModel):
    """The one endpoint that returns a raw phone number, and it is audited."""

    dindi_id: uuid.UUID
    code: str
    leader_name: str
    leader_phone: str
    notice: str
    notice_mr: str


# ---------------------------------------------------------------------------
# halt towns
# ---------------------------------------------------------------------------
class ArrivingOut(ApiModel):
    dindi_id: uuid.UUID
    code: str
    name: str
    name_mr: str
    expected_count: int
    planned_arrival: datetime
    eta: datetime | None = None
    deviation_minutes: float | None = None
    is_signal_lost: bool = False


class ReadinessOut(ApiModel):
    expected_headcount: int
    water_points: int
    water_points_required: int
    sanitation_units: int
    sanitation_units_required: int
    medical_camps: int
    medical_camps_required: int
    #: What the numbers support.
    computed: str
    #: What a coordinator typed in.
    declared: str
    #: True when a town is marked ready and the provisioning does not support
    #: it. This is the single most useful field on the board — collapsing the
    #: two statuses into one would hide exactly the case Section 4/M8 exists to
    #: catch.
    disagrees: bool = False
    gaps: list[str] = Field(default_factory=list)
    gaps_mr: list[str] = Field(default_factory=list)
    #: The ratios the assessment used, served rather than hardcoded in the UI so
    #: a changed convention shows up in the output instead of hiding behind it.
    basis: str
    basis_mr: str


class HaltTownOut(ApiModel):
    id: uuid.UUID
    name: str
    name_mr: str
    route_id: uuid.UUID | None = None
    route_name: str | None = None
    sequence: int
    centroid: tuple[float, float] | None = None
    readiness: ReadinessOut
    readiness_note: str | None = None
    readiness_updated_at: datetime | None = None
    #: Earliest expected arrival among the groups walking towards it — the
    #: measured ETA where there is one, the plan where there is not.
    first_arrival_expected: datetime | None = None
    arriving: list[ArrivingOut] = Field(default_factory=list)


class HaltTownBoardOut(ApiModel):
    towns: list[HaltTownOut]
    generated_at: datetime
    within_hours: int
    notice: str
    notice_mr: str


class ReadinessUpdate(ApiModel):
    water_points: int | None = Field(default=None, ge=0, le=10_000)
    sanitation_units: int | None = Field(default=None, ge=0, le=10_000)
    medical_camps: int | None = Field(default=None, ge=0, le=1_000)
    #: ready | partial | not_ready | unknown
    readiness_status: str | None = Field(default=None, max_length=16)
    readiness_note: str | None = Field(default=None, max_length=2_000)
    expected_arrival: datetime | None = None
