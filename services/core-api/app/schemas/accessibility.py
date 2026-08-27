"""Accessibility and assistance schemas (Track 1, item 4).

One shape rule runs through this file: **needs travel, notes do not.**

`AssistanceRequestOut` is what the volunteer board reads, and it carries the
needs list — "wheelchair", "step_free_route" — because that is what a volunteer
acts on. It does not carry the pilgrim's `notes`, which are their own words
about their body. Those come back only on their own profile read, to them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.models.accessibility import AssistanceNeed, RequestStatus
from app.schemas.common import ApiModel


class AccessibilityProfileIn(ApiModel):
    """What a pilgrim declares once, and never again.

    Every field is optional and the whole row is replaced on write: the screen
    shows all of it at once, so an unticked box means "no longer needed" rather
    than "unchanged".
    """

    needs: list[AssistanceNeed] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=1000)
    large_text: bool = False
    high_contrast: bool = False
    #: Somebody to reach who is not the pilgrim. Hashed before storage.
    companion_phone: str | None = Field(default=None, max_length=20)


class AccessibilityProfileOut(ApiModel):
    needs: list[AssistanceNeed] = Field(default_factory=list)
    notes: str | None = None
    large_text: bool = False
    high_contrast: bool = False
    has_companion_contact: bool = False
    #: Whether this profile opens the reserved darshan seats. Surfaced so the
    #: booking screen can say *why* a slot that looks full is offering a place,
    #: rather than appearing to break its own rules.
    priority_booking: bool = False
    updated_at: datetime | None = None


class AssistanceRequestCreate(ApiModel):
    """An ask for help, now.

    `needs` may be empty: the server fills it from the caller's profile. A
    pilgrim stuck at a step should be able to press one button, not re-declare
    a disability they have already recorded.
    """

    needs: list[AssistanceNeed] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)
    zone_id: uuid.UUID | None = None
    gate_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None
    #: A volunteer raising one for somebody in front of them — the person who
    #: needs the chair is very often not the person holding a phone.
    on_behalf_of: str | None = Field(default=None, max_length=120)
    language: str = "mr"
    #: Set by the offline queue to the moment the pilgrim pressed the button.
    #: The SLA clock starts here, not at the moment the phone found a signal.
    client_reported_at: datetime | None = None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        return value if value in {"mr", "hi", "en"} else "mr"


class AssistanceRequestOut(ApiModel):
    id: uuid.UUID
    reference: str
    needs: list[AssistanceNeed] = Field(default_factory=list)
    note: str | None = None
    on_behalf_of: str | None = None
    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    gate_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None
    status: RequestStatus
    requested_at: datetime
    sla_due_at: datetime
    assigned_at: datetime | None = None
    resolved_at: datetime | None = None
    outcome_note: str | None = None
    language: str = "mr"
    #: Late means nobody was *assigned* in time — not that the help has not
    #: finished. A volunteer still pushing the chair has breached nothing.
    sla_breached: bool = False
    waiting_seconds: float = 0.0


class AssistanceUpdate(ApiModel):
    status: RequestStatus | None = None
    #: Mandatory to close as `unmet`, enforced in the service rather than here,
    #: because the rule is about the transition and not about the payload.
    outcome_note: str | None = Field(default=None, max_length=1000)
    #: Take it yourself. The board has no "assign to somebody else" — a
    #: volunteer who is not there cannot be volunteered by a screen.
    claim: bool = False


class SlotAccessibility(ApiModel):
    """The reserved-seat picture for one slot, for the booking screen."""

    assisted_reserve: int = 0
    assisted_used: int = 0
    assisted_available: int = 0


class FacilityAccessibilityIn(ApiModel):
    """A field survey result.

    Unknown keys are dropped rather than stored — a typo like `step-free` would
    otherwise persist as a key nothing reads, and the facility would show as
    unsurveyed forever while looking filled in.
    """

    step_free: bool | None = None
    ramp: bool | None = None
    accessible_toilet: bool | None = None
    seating: bool | None = None
    staffed: bool | None = None
    handrail: bool | None = None
