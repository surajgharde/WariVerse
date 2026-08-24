"""Incident, SOS, dispatch and missing-person schemas (Section 4/M4).

Two things shape every model here.

**A reporter is not stored as a person.** An incident carries `reported_by`
(a user id, when one is signed in) and `reporter_phone_hash` — never a raw
number. The raw number lives only in the encrypted contact table, and only for
as long as there is a reason to call back. Section 12's data minimisation rule
is not a policy this module respects; it is a shape it is built out of.

**The pilgrim-facing response says what it knows and admits what it does not.**
Section 4/M4: "Shows the pilgrim a confirmation with a reference number and, if
available, the ETA of the nearest responder. Never leave them staring at a
spinner." So `SosAck` always carries a reference, and `responder_eta_seconds` is
nullable with a Marathi line explaining the null — a missing ETA is answered
with "help has been told", not with a blank field.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.models.incidents import IncidentSeverity, IncidentStatus, IncidentType
from app.schemas.common import ApiModel

#: Where a report came from. Kept as a Literal rather than an enum on the model
#: because an operator logging a phone call is a different provenance from a
#: pilgrim pressing a button, and post-Wari review turns on that difference.
#:
#: `pilgrim_report` is separate from `pilgrim_sos` for a reason that is not
#: bookkeeping: `incident_service._open_sos_for` finds a caller's open SOS by
#: `source == "pilgrim_sos"`, so filing a lost-umbrella report under that source
#: would make the pilgrim's *next* panic press attach itself to the umbrella.
#: The route never lets a client choose its own source — see `_resolve_source`.
IncidentSource = Literal[
    "pilgrim_sos", "pilgrim_report", "volunteer_report", "ai_alert", "control_room", "phone_call"
]

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
class IncidentCreate(ApiModel):
    """Control-room entry, volunteer report, or a logged phone call."""

    type: IncidentType
    severity: IncidentSeverity
    source: IncidentSource = "control_room"
    zone_id: uuid.UUID | None = None
    #: [lon, lat]. GeoJSON order, matching every other coordinate in the API.
    location: Point | None = None
    description: str | None = Field(default=None, max_length=2000)
    #: For a phone call: how to reach the caller back. Stored encrypted with a
    #: TTL, never in the incident row.
    contact_phone: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _locatable(self) -> IncidentCreate:
        # An incident nobody can find is an incident nobody can attend. One of
        # the two is required — a zone at minimum, because "somewhere in the
        # temple" is not a dispatchable fact.
        if self.zone_id is None and self.location is None:
            raise ValueError("Either zone_id or location is required — a responder has to be sent somewhere")
        return self


class SosCreate(ApiModel):
    """The pilgrim's panic button.

    Every field except the coordinates is optional, on purpose. Someone pressing
    this is not filling in a form, and a required field is a reason for the
    request to fail at the moment it must not.
    """

    location: Point | None = None
    zone_id: uuid.UUID | None = None
    type: IncidentType = IncidentType.MEDICAL
    description: str | None = Field(default=None, max_length=1000)
    #: Where a 10-second voice note was stored. The audio itself never passes
    #: through this API.
    audio_note_uri: str | None = Field(default=None, max_length=500)
    #: When the pilgrim actually pressed the button. Set by the client when the
    #: SOS was queued offline and is only now reaching us — Section 4/M7's
    #: offline queue. Without it an operator reads a 20-minute-old emergency as
    #: brand new.
    client_reported_at: datetime | None = None


class SosAck(ApiModel):
    """What the pilgrim's phone shows the moment the SOS lands."""

    incident_id: uuid.UUID
    reference: str
    status: IncidentStatus
    #: Always populated, in both languages. A confirmation the pilgrim cannot
    #: read is not a confirmation.
    message: str
    message_mr: str
    #: Null when no unit has been assigned yet, which is the normal case — a
    #: human confirms every dispatch. The message says so rather than leaving
    #: the field to speak for itself.
    responder_eta_seconds: float | None = None
    responder_call_sign: str | None = None
    #: True when this attached to an SOS the caller already had open instead of
    #: opening a second one. The button still "worked"; it did not duplicate.
    joined_existing: bool = False
    received_at: datetime


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
class IncidentEventOut(ApiModel):
    """One line of an incident's append-only timeline."""

    id: uuid.UUID
    action: str
    note: str | None = None
    actor_id: uuid.UUID | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class IncidentOut(ApiModel):
    id: uuid.UUID
    reference: str
    type: IncidentType
    severity: IncidentSeverity
    status: IncidentStatus
    source: str

    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    zone_name_mr: str | None = None
    location: Point | None = None

    description: str | None = None
    has_audio_note: bool = False

    #: SLA, as three separate facts rather than one traffic light. An operator
    #: triaging a queue needs to know how long is left, not just whether the
    #: clock has already run out.
    sla_due_at: datetime
    sla_breached: bool
    seconds_to_sla: float
    first_response_at: datetime | None = None

    assigned_responder_id: uuid.UUID | None = None
    assigned_call_sign: str | None = None

    #: Present when the report was queued offline and arrived late. The console
    #: shows the delay, because a 20-minute-old SOS is not a new one.
    client_reported_at: datetime | None = None
    delayed_by_seconds: float | None = None

    alert_id: uuid.UUID | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    outcome_note: str | None = None
    created_at: datetime
    seconds_open: float

    #: Only on the single-incident read; the list view leaves it empty.
    timeline: list[IncidentEventOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# acting
# ---------------------------------------------------------------------------
class IncidentUpdate(ApiModel):
    """Move an incident along, or re-grade it.

    `outcome_note` is required to reach `closed` and the service enforces it —
    see `OUTCOME_NOTE_REQUIRED`. A closed incident with no record of what was
    done is the one an inquiry will ask about.
    """

    status: IncidentStatus | None = None
    severity: IncidentSeverity | None = None
    note: str | None = Field(default=None, max_length=1000)
    outcome_note: str | None = Field(default=None, max_length=2000)


class DispatchRequest(ApiModel):
    """Send a named unit. A human picked it; the server records who."""

    responder_id: uuid.UUID
    note: str | None = Field(default=None, max_length=500)
    #: Set when the operator is dispatching a unit the ranking did not suggest.
    #: Not a blocker — the person on the radio knows things the map does not —
    #: but it is recorded, because a pattern of overrides is worth seeing.
    override_reason: str | None = Field(default=None, max_length=500)


class SuggestionOut(ApiModel):
    responder_id: uuid.UUID
    call_sign: str
    unit_type: str
    #: Straight-line metres. Null when either end has no known position.
    distance_m: float | None = None
    #: Walking time through a crowd at 0.7 m/s. An estimate and a floor, not a
    #: promise — the real route is longer and may be blocked.
    eta_seconds: float | None = None
    type_rank: int
    caveats: list[str] = Field(default_factory=list)


class DispatchOptions(ApiModel):
    incident_id: uuid.UUID
    suggestions: list[SuggestionOut]
    #: Stated so an empty list is never read as "no units exist".
    available_units: int
    note: str
    note_mr: str
    generated_at: datetime


class ResponderOut(ApiModel):
    id: uuid.UUID
    call_sign: str
    unit_type: str
    status: str
    location: Point | None = None
    last_ping_at: datetime | None = None
    seconds_since_ping: float | None = None
    #: The incident this unit is currently on, if any.
    assigned_incident_id: uuid.UUID | None = None
    assigned_incident_reference: str | None = None


class ResponderPing(ApiModel):
    """A unit reporting where it is."""

    location: Point
    status: Literal["available", "assigned", "on_scene", "off_duty"] | None = None


# ---------------------------------------------------------------------------
# missing persons (Section 5, E2)
# ---------------------------------------------------------------------------
class MissingPersonCreate(ApiModel):
    """The highest-frequency real incident at the Wari.

    `photo_uri` points at an object store; the image never passes through this
    API. It is purged 30 days after the case closes, and `purge_after` is set at
    closure rather than at report — a case still open at day 31 has not stopped
    needing the photo.
    """

    name: str = Field(min_length=1, max_length=120)
    age: int | None = Field(default=None, ge=0, le=120)
    description: str | None = Field(default=None, max_length=1000)
    photo_uri: str | None = Field(default=None, max_length=500)
    last_seen_zone_id: uuid.UUID | None = None
    last_seen_at: datetime | None = None
    #: The person to call when they are found. Hashed for the row, encrypted for
    #: the callback, never stored in the clear on the case.
    contact_phone: str = Field(min_length=6, max_length=20)
    language: str = Field(default="mr", max_length=5)


class MissingPersonOut(ApiModel):
    id: uuid.UUID
    incident_id: uuid.UUID | None = None
    incident_reference: str | None = None
    name: str
    age: int | None = None
    description: str | None = None
    #: Whether a photo exists — not where it is. The URI is only handed out on
    #: the single-case read, to a role that may see it.
    has_photo: bool = False
    last_seen_zone_id: uuid.UUID | None = None
    last_seen_zone_code: str | None = None
    last_seen_at: datetime | None = None
    language: str
    status: str
    reported_at: datetime
    resolved_at: datetime | None = None
    purge_after: datetime | None = None
    #: How long this person has been missing. The number that decides the order
    #: an announcement desk works the list in.
    open_for_seconds: float


class MissingPersonUpdate(ApiModel):
    status: Literal["open", "sighted", "reunited", "closed_unresolved"]
    note: str | None = Field(default=None, max_length=1000)
    #: Where they were seen or found, so the timeline records more than a state.
    zone_id: uuid.UUID | None = None
