"""Dispatch arithmetic (Section 4/M4).

Pure functions. Given a set of responder units and an incident location, rank
the units a human might send — and stop there.

**Nothing in this module dispatches anything.** Section 4/M4 is explicit: "Auto-
suggest the nearest available responder unit by type and haversine distance, but
a human confirms. No auto-dispatch." So this file computes a suggestion and the
route requires an operator to act on it. That separation is the whole point:
the nearest ambulance by straight-line distance may be on the wrong side of a
closed gate, or already walking to something the system has not been told about,
and the person on the radio knows that where the arithmetic does not.

The ETA is the number most likely to be believed and least likely to be right,
so it is computed conservatively and labelled as what it is — see `walk_eta`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt

from app.models.incidents import IncidentSeverity, IncidentType

#: Mean Earth radius in metres.
EARTH_RADIUS_M = 6_371_000.0

#: Which unit types are worth suggesting for which incident, best first.
#: A medical incident wants an ambulance before a medical team on foot; a
#: crush risk wants bodies on the ground before it wants a vehicle that cannot
#: get through the crowd it is being sent into.
UNIT_PREFERENCE: dict[IncidentType, tuple[str, ...]] = {
    IncidentType.MEDICAL: ("ambulance", "medical_team", "volunteer_squad"),
    IncidentType.MISSING_PERSON: ("help_desk", "volunteer_squad", "police"),
    IncidentType.CROWD_CRUSH_RISK: ("volunteer_squad", "police", "medical_team"),
    IncidentType.FIRE: ("fire", "police", "medical_team"),
    IncidentType.STRUCTURAL: ("fire", "police", "volunteer_squad"),
    IncidentType.LOST_ITEM: ("help_desk", "volunteer_squad"),
    IncidentType.FACILITY_FAILURE: ("volunteer_squad", "help_desk"),
    IncidentType.SECURITY: ("police", "volunteer_squad"),
    IncidentType.OTHER: ("volunteer_squad", "police", "medical_team", "help_desk"),
}

#: Walking speed through a Wari crowd, metres per second.
#:
#: 0.7 m/s, not the 1.4 m/s of an empty pavement. A responder crossing a zone at
#: 4 p/m² is not walking, they are negotiating. Halving the free-flow figure is
#: still optimistic, and being optimistic here means an ETA that arrives before
#: the responder does — which is why the caller is told this is an estimate.
CROWD_WALK_SPEED_MS = 0.7

#: Straight-line distance beyond which a suggestion is not worth making.
#: A unit 2 km away is not "the nearest available unit", it is a different
#: part of the operation.
MAX_SUGGEST_DISTANCE_M = 2_000.0


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lon, lat) points."""
    lon1, lat1 = a
    lon2, lat2 = b
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    h = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(min(1.0, h)))


def walk_eta(distance_m: float, *, speed_ms: float = CROWD_WALK_SPEED_MS) -> timedelta:
    """Time to cover `distance_m` on foot through a crowd.

    Straight-line distance, so this is a floor rather than a forecast: the real
    route is longer, and it may be blocked. The console labels it "estimate" and
    the pilgrim-facing text says "if available" for exactly this reason —
    Section 4/M4 wants the pilgrim to see an ETA, not to be promised one.
    """
    if speed_ms <= 0:
        raise ValueError("speed must be positive")
    return timedelta(seconds=distance_m / speed_ms)


def sla_due(severity: IncidentSeverity | str, *, from_time, minutes: dict | None = None):
    """When first responder contact is due for this severity."""
    from app.models.incidents import SLA_MINUTES

    table = minutes or SLA_MINUTES
    key = severity if isinstance(severity, IncidentSeverity) else IncidentSeverity(severity)
    return from_time + timedelta(minutes=table[key])


@dataclass(frozen=True, slots=True)
class ResponderCandidate:
    """One unit, as the ranker needs to see it."""

    responder_id: uuid.UUID
    call_sign: str
    unit_type: str
    status: str
    location: tuple[float, float] | None
    seconds_since_ping: float | None


@dataclass(frozen=True, slots=True)
class Suggestion:
    responder_id: uuid.UUID
    call_sign: str
    unit_type: str
    distance_m: float | None
    eta_seconds: float | None
    #: Rank of this unit's type for this incident type; 0 is the best match.
    type_rank: int
    #: Why an operator might hesitate. Empty means nothing to flag.
    caveats: list[str]


#: A unit whose last ping is older than this is stale — it may not be where the
#: map thinks. Suggested anyway, but flagged, because a stale unit that is
#: actually nearby beats no suggestion at all.
STALE_PING_SECONDS = 300.0


def suggest(
    candidates: list[ResponderCandidate],
    *,
    incident_type: IncidentType | str,
    incident_location: tuple[float, float] | None,
    limit: int = 5,
) -> list[Suggestion]:
    """Rank units for an operator to choose from.

    Ordering is by *type fit first, then distance* — deliberately. A volunteer
    squad standing next to a cardiac arrest is closer than the ambulance and is
    not the right answer; sorting by distance alone would put it top and invite
    the wrong click under time pressure. Units of an unpreferred type are still
    returned, ranked last, because at 2 a.m. the wrong unit that exists beats the
    right unit that does not.

    Units with no known location are included with `distance_m = None` rather
    than dropped. "We do not know where this unit is" is information an operator
    can act on; silently omitting it makes the roster look smaller than it is.
    """
    itype = incident_type if isinstance(incident_type, IncidentType) else IncidentType(incident_type)
    preference = UNIT_PREFERENCE.get(itype, UNIT_PREFERENCE[IncidentType.OTHER])

    out: list[Suggestion] = []
    for unit in candidates:
        if unit.status != "available":
            continue

        try:
            type_rank = preference.index(unit.unit_type)
        except ValueError:
            # Not a preferred type for this incident: rank after every preferred
            # one, but keep it on the list.
            type_rank = len(preference)

        distance: float | None = None
        eta: float | None = None
        caveats: list[str] = []

        if incident_location is not None and unit.location is not None:
            distance = haversine_m(unit.location, incident_location)
            if distance > MAX_SUGGEST_DISTANCE_M:
                continue
            eta = walk_eta(distance).total_seconds()
        elif unit.location is None:
            caveats.append("no known position for this unit")
        else:
            caveats.append("the incident has no location, so distance is unknown")

        if unit.seconds_since_ping is None:
            caveats.append("this unit has never reported its position")
        elif unit.seconds_since_ping > STALE_PING_SECONDS:
            caveats.append(f"position is {int(unit.seconds_since_ping // 60)} minutes old")

        if type_rank == len(preference):
            caveats.append(f"{unit.unit_type} is not a usual unit for {itype}")

        out.append(
            Suggestion(
                responder_id=unit.responder_id,
                call_sign=unit.call_sign,
                unit_type=unit.unit_type,
                distance_m=round(distance, 1) if distance is not None else None,
                eta_seconds=round(eta, 1) if eta is not None else None,
                type_rank=type_rank,
                caveats=caveats,
            )
        )

    # Units with an unknown distance sort after every unit with a known one, at
    # the same type rank — an operator should see the measurable options first.
    out.sort(key=lambda s: (s.type_rank, s.distance_m is None, s.distance_m or 0.0, s.call_sign))
    return out[:limit]
