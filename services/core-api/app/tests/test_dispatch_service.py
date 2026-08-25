"""Dispatch ranking and SLA arithmetic (Section 4/M4).

Pure functions, no database. The rules being pinned here are the ones that
decide what an operator's eye lands on first under time pressure, so they are
worth testing at the level where the decision is actually made.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.incidents import SLA_MINUTES, IncidentSeverity, IncidentType
from app.services import dispatch_service, incident_service

#: Two points about 100 m apart near the Vitthal temple in Pandharpur.
NEAR = (75.3300, 17.6790)
FAR = (75.3310, 17.6790)


def unit(
    *,
    call_sign: str,
    unit_type: str,
    location: tuple[float, float] | None = NEAR,
    status: str = "available",
    seconds_since_ping: float | None = 10.0,
) -> dispatch_service.ResponderCandidate:
    return dispatch_service.ResponderCandidate(
        responder_id=uuid.uuid4(),
        call_sign=call_sign,
        unit_type=unit_type,
        status=status,
        location=location,
        seconds_since_ping=seconds_since_ping,
    )


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def test_haversine_matches_a_known_degree_of_latitude():
    """One degree of latitude is ~111.2 km anywhere on Earth."""
    metres = dispatch_service.haversine_m((75.0, 17.0), (75.0, 18.0))
    assert 110_500 < metres < 111_500


def test_haversine_is_symmetric_and_zero_at_a_point():
    assert dispatch_service.haversine_m(NEAR, FAR) == pytest.approx(
        dispatch_service.haversine_m(FAR, NEAR)
    )
    assert dispatch_service.haversine_m(NEAR, NEAR) == pytest.approx(0.0, abs=1e-6)


def test_walk_eta_uses_crowd_speed_not_free_flow():
    """0.7 m/s, not the 1.4 m/s of an empty pavement.

    A responder crossing a zone at 4 p/m² is not walking, they are negotiating.
    If this ever silently doubles, every ETA in the system halves.
    """
    assert dispatch_service.walk_eta(70.0).total_seconds() == pytest.approx(100.0)
    assert dispatch_service.CROWD_WALK_SPEED_MS == 0.7


def test_walk_eta_refuses_a_nonsense_speed():
    with pytest.raises(ValueError):
        dispatch_service.walk_eta(100.0, speed_ms=0.0)


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------
def test_type_fit_beats_raw_distance():
    """The single most important ordering rule in the module.

    A volunteer squad standing next to a cardiac arrest is closer than the
    ambulance and is not the right answer. Sorting by distance alone puts it top
    and invites the wrong click under time pressure.
    """
    squad = unit(call_sign="V-1", unit_type="volunteer_squad", location=NEAR)
    ambulance = unit(call_sign="A-1", unit_type="ambulance", location=FAR)

    ranked = dispatch_service.suggest(
        [squad, ambulance], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )

    assert [s.call_sign for s in ranked] == ["A-1", "V-1"]
    # The ambulance ranks first *despite* being further away — which is the
    # whole point, so assert the distances really do run the other way.
    assert ranked[0].distance_m is not None and ranked[1].distance_m is not None
    assert ranked[0].distance_m > ranked[1].distance_m


def test_distance_breaks_ties_within_a_type():
    near = unit(call_sign="A-near", unit_type="ambulance", location=NEAR)
    far = unit(call_sign="A-far", unit_type="ambulance", location=FAR)

    ranked = dispatch_service.suggest(
        [far, near], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )
    assert [s.call_sign for s in ranked] == ["A-near", "A-far"]


def test_a_unit_of_the_wrong_type_is_ranked_last_but_still_offered():
    """At 2 a.m. the wrong unit that exists beats the right unit that does not."""
    desk = unit(call_sign="H-1", unit_type="help_desk", location=NEAR)

    ranked = dispatch_service.suggest(
        [desk], incident_type=IncidentType.FIRE, incident_location=NEAR
    )

    assert [s.call_sign for s in ranked] == ["H-1"]
    assert ranked[0].type_rank == len(dispatch_service.UNIT_PREFERENCE[IncidentType.FIRE])
    assert any("not a usual unit" in c for c in ranked[0].caveats)


def test_a_unit_with_no_known_position_is_offered_with_a_null_distance():
    """"We do not know where this unit is" is information an operator can act
    on; silently omitting it makes the roster look smaller than it is."""
    lost = unit(call_sign="A-lost", unit_type="ambulance", location=None)
    known = unit(call_sign="A-known", unit_type="ambulance", location=NEAR)

    ranked = dispatch_service.suggest(
        [lost, known], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )

    assert [s.call_sign for s in ranked] == ["A-known", "A-lost"]
    assert ranked[1].distance_m is None
    assert ranked[1].eta_seconds is None
    assert any("no known position" in c for c in ranked[1].caveats)


def test_a_unit_beyond_the_cutoff_is_not_a_suggestion():
    """A unit 2 km away is not "the nearest available unit", it is a different
    part of the operation."""
    # ~0.05 degrees of longitude at this latitude is roughly 5.3 km.
    distant = unit(call_sign="A-town", unit_type="ambulance", location=(75.3800, 17.6790))

    ranked = dispatch_service.suggest(
        [distant], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )
    assert ranked == []


def test_an_unavailable_unit_is_never_suggested():
    for status in ("assigned", "on_scene", "off_duty"):
        busy = unit(call_sign="A-1", unit_type="ambulance", status=status)
        assert (
            dispatch_service.suggest(
                [busy], incident_type=IncidentType.MEDICAL, incident_location=NEAR
            )
            == []
        )


def test_a_stale_position_is_flagged_rather_than_dropped():
    """A stale unit that is actually nearby beats no suggestion at all."""
    stale = unit(
        call_sign="A-1",
        unit_type="ambulance",
        seconds_since_ping=dispatch_service.STALE_PING_SECONDS + 60,
    )

    ranked = dispatch_service.suggest(
        [stale], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )

    assert len(ranked) == 1
    assert any("minutes old" in c for c in ranked[0].caveats)


def test_a_unit_that_has_never_pinged_says_so():
    never = unit(call_sign="A-1", unit_type="ambulance", seconds_since_ping=None)
    ranked = dispatch_service.suggest(
        [never], incident_type=IncidentType.MEDICAL, incident_location=NEAR
    )
    assert any("never reported" in c for c in ranked[0].caveats)


def test_an_incident_with_no_location_ranks_by_type_and_says_distance_is_unknown():
    ambulance = unit(call_sign="A-1", unit_type="ambulance")
    squad = unit(call_sign="V-1", unit_type="volunteer_squad")

    ranked = dispatch_service.suggest(
        [squad, ambulance], incident_type=IncidentType.MEDICAL, incident_location=None
    )

    assert [s.call_sign for s in ranked] == ["A-1", "V-1"]
    assert all(s.distance_m is None for s in ranked)
    assert all(any("no location" in c for c in s.caveats) for s in ranked)


def test_limit_is_honoured():
    units = [unit(call_sign=f"A-{i}", unit_type="ambulance") for i in range(10)]
    ranked = dispatch_service.suggest(
        units, incident_type=IncidentType.MEDICAL, incident_location=NEAR, limit=3
    )
    assert len(ranked) == 3


def test_every_incident_type_has_a_preference_list():
    """A type added to the model without a preference list would silently fall
    through to OTHER and rank every unit equally."""
    for incident_type in IncidentType:
        assert incident_type in dispatch_service.UNIT_PREFERENCE


# ---------------------------------------------------------------------------
# SLA arithmetic
# ---------------------------------------------------------------------------
def test_sla_windows_match_the_spec():
    """Section 4/M4: critical 3 min, high 10 min, normal 30 min."""
    assert SLA_MINUTES[IncidentSeverity.CRITICAL] == 3
    assert SLA_MINUTES[IncidentSeverity.HIGH] == 10
    assert SLA_MINUTES[IncidentSeverity.NORMAL] == 30


def test_sla_due_at_is_measured_from_the_report_not_from_now():
    reported = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
    assert incident_service.sla_due_at(IncidentSeverity.CRITICAL, at=reported) == reported + timedelta(
        minutes=3
    )
    assert incident_service.sla_due_at("high", at=reported) == reported + timedelta(minutes=10)


def test_there_is_only_one_sla_clock():
    """Two functions computing when a responder is due is two places to get it
    wrong, and the one that drifts is always the copy nobody remembered.

    This started as a test that the two helpers agreed. Asserting agreement
    between duplicates is a weaker guarantee than not having duplicates — the
    second one can always be added back with a slightly different rounding rule
    and a test that passes for a while. So the duplicate was deleted and this
    test guards its absence.
    """
    assert not hasattr(dispatch_service, "sla_due"), (
        "the SLA clock belongs to incident_service.sla_due_at alone"
    )
    assert callable(incident_service.sla_due_at)


def test_reference_codes_avoid_characters_that_get_misheard_on_a_radio():
    """No I, O, 0 or 1 — "INC-I0" is a reference nobody transcribes twice."""
    codes = {incident_service.reference() for _ in range(200)}
    assert all(code.startswith("INC-") for code in codes)
    assert all(len(code) == 10 for code in codes)
    assert not any(set("IO01") & set(code[4:]) for code in codes)
    # 32^6 possibilities: 200 draws colliding would mean the alphabet shrank.
    assert len(codes) == 200
