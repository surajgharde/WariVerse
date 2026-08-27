"""Seed a development database with the Pandharpur temple complex.

Idempotent: safe to re-run.  Creates the staff accounts the demo script needs,
the zones the crowd engine will publish into, gates, cameras and facilities.

    python scripts/seed_dev.py

Real Pandharpur coordinates are used so the map is honest from the first run;
zone polygons are approximate and are meant to be replaced by surveyed
geometry before any deployment (the area_m2 figure drives every density
calculation, so it is a calibration input, not decoration).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory, dispose_engine
from app.core.permissions import Role, requires_mfa
from app.core.security import hash_password, hash_phone, normalise_phone, now_utc
from app.models import Camera, Facility, Gate, Responder, User, Zone
from app.services.calibration import solve_homography

# Shri Vitthal-Rukmini Temple, Pandharpur.
TEMPLE_LON, TEMPLE_LAT = 75.3306, 17.6797

DEMO_PASSWORD = "wari-demo-2026-change-me"

STAFF = [
    ("9000000001", "सिस्टम प्रशासक", Role.SYSTEM_ADMIN),
    ("9000000002", "मंदिर प्रशासक", Role.ADMINISTRATOR),
    ("9000000003", "सुरक्षा अधिकारी", Role.SECURITY_OFFICER),
    ("9000000004", "स्वयंसेवक", Role.VOLUNTEER),
    ("9000000005", "वैद्यकीय पथक", Role.RESPONDER),
]


#: (call_sign, unit_type, d_lon, d_lat) — spread across the complex rather than
#: parked on the temple, so the distance ranking has something to rank.
RESPONDERS = [
    ("AMB-1", "ambulance", 0.0011, 0.0004),
    ("AMB-2", "ambulance", -0.0036, 0.0015),
    ("MED-1", "medical_team", 0.0001, 0.0001),
    ("MED-2", "medical_team", -0.0015, -0.0004),
    ("POL-1", "police", 0.0012, 0.0005),
    ("POL-2", "police", -0.0037, 0.0016),
    ("FIRE-1", "fire", 0.0014, 0.0007),
    ("VOL-1", "volunteer_squad", -0.0016, -0.0004),
    ("VOL-2", "volunteer_squad", -0.0038, 0.0016),
    ("VOL-3", "volunteer_squad", 0.0002, 0.0002),
    ("DESK-1", "help_desk", 0.0012, 0.0004),
    ("DESK-2", "help_desk", -0.0036, 0.0014),
]


def _box(lon: float, lat: float, half_lon: float, half_lat: float) -> str:
    """A rectangular WKT polygon around a point."""
    w, e = lon - half_lon, lon + half_lon
    s, n = lat - half_lat, lat + half_lat
    return f"SRID=4326;POLYGON(({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}))"


# (code, name, name_mr, zone_type, area_m2, capacity, half_lon, half_lat, d_lon, d_lat)
ZONES = [
    ("TC", "Temple Core", "मंदिर गाभारा", "temple_core", 1200.0, 2400, 0.00025, 0.00025, 0.0, 0.0),
    ("QC", "Queue Corridor", "रांग मार्गिका", "queue", 4800.0, 12000, 0.0009, 0.0004, -0.0016, -0.0004),
    ("NW", "Namdev Wari Gate Plaza", "नामदेव पायरी चौक", "corridor", 2600.0, 6000, 0.0006, 0.0004, 0.0012, 0.0005),
    ("CG", "Chandrabhaga Ghat", "चंद्रभागा घाट", "ghat", 15000.0, 30000, 0.0022, 0.0009, -0.0038, 0.0016),
    ("AR", "Approach Road North", "उत्तर प्रवेश मार्ग", "approach_road", 8000.0, 14000, 0.0018, 0.0006, 0.0008, 0.0032),
    ("RZ", "Rest Zone East", "विश्रांती क्षेत्र", "facility", 6000.0, 9000, 0.0012, 0.0008, 0.0042, -0.0006),
]

# (code, name, name_mr, zone_code, restricted, throughput, d_lon, d_lat)
GATES = [
    ("G1", "Main Darshan Gate", "मुख्य दर्शन द्वार", "TC", False, 3000, 0.0002, 0.0),
    ("G2", "Queue Intake Gate", "रांग प्रवेश द्वार", "QC", False, 2500, -0.0024, -0.0004),
    ("G3", "VIP / Restricted Side Entry", "विशेष प्रवेश (प्रतिबंधित)", "TC", True, 400, -0.0002, 0.0003),
    ("G4", "Exit Gate", "निर्गमन द्वार", "NW", False, 3200, 0.0016, 0.0005),
]

# (name, zone_code, tripwire_enabled)
CAMERAS = [
    ("CAM-TC-01", "TC", False),
    ("CAM-QC-01", "QC", False),
    ("CAM-QC-02", "QC", False),
    ("CAM-G3-01", "TC", True),  # watches the restricted side entry
    ("CAM-CG-01", "CG", False),
    ("CAM-AR-01", "AR", False),
]

# (type, name, name_mr, zone_code, d_lon, d_lat)
FACILITIES = [
    ("water", "Water Point A", "पाणी केंद्र अ", "QC", -0.0020, -0.0002),
    ("toilet", "Sanitation Block 1", "स्वच्छतागृह १", "QC", -0.0026, -0.0006),
    ("medical", "Medical Camp Ghat", "वैद्यकीय शिबिर घाट", "CG", -0.0040, 0.0018),
    ("medical", "Medical Camp Temple", "वैद्यकीय शिबिर मंदिर", "NW", 0.0014, 0.0006),
    ("food", "Annachhatra", "अन्नछत्र", "RZ", 0.0044, -0.0008),
    ("rest_zone", "Rest Shelter East", "विश्रांती निवारा", "RZ", 0.0040, -0.0004),
    ("lost_and_found", "Lost & Found Desk", "हरवले-सापडले कक्ष", "NW", 0.0010, 0.0004),
    ("help_desk", "Help Desk North", "मदत कक्ष उत्तर", "AR", 0.0006, 0.0030),
]


async def seed_users(session: AsyncSession) -> int:
    created = 0
    enrolled = 0
    for phone, name, role in STAFF:
        phone_hash = hash_phone(phone)
        existing = await session.scalar(select(User).where(User.phone_hash == phone_hash))
        if existing is not None:
            # Backfill, rather than skip. The seed is run repeatedly against a
            # database that already has these accounts, and a fix that only
            # applies to freshly created rows is a fix nobody already running the
            # stack ever receives. An Administrator seeded before this existed
            # still cannot sign in, and re-running the seed is the obvious thing
            # somebody would try.
            if requires_mfa(existing.role) and not existing.mfa_secret:
                existing.mfa_secret = settings.dev_mfa_secret
                enrolled += 1
            continue
        session.add(
            User(
                phone=normalise_phone(phone),
                phone_hash=phone_hash,
                name=name,
                role=role,
                language="mr",
                password_hash=hash_password(DEMO_PASSWORD),
                # Administrator and System Admin cannot sign in at all without an
                # enrolled secret — `login_with_password` refuses an MFA role that
                # has none, and `/auth/mfa/enrol` needs a token you can only get by
                # signing in. Seeding a fixed development secret breaks that
                # deadlock. It is the same value on every dev machine and is
                # useless anywhere real, because production seeds nothing.
                mfa_secret=settings.dev_mfa_secret if requires_mfa(role) else None,
                is_active=True,
            )
        )
        created += 1
    await session.flush()
    if enrolled:
        print(f"  backfilled the development MFA secret onto {enrolled} existing account(s)")
    return created


async def seed_zones(session: AsyncSession) -> dict[str, Zone]:
    zones: dict[str, Zone] = {}
    for code, name, name_mr, ztype, area, capacity, hlon, hlat, dlon, dlat in ZONES:
        zone = await session.scalar(select(Zone).where(Zone.code == code))
        if zone is None:
            zone = Zone(
                code=code,
                name=name,
                name_mr=name_mr,
                geom=_box(TEMPLE_LON + dlon, TEMPLE_LAT + dlat, hlon, hlat),
                area_m2=area,
                capacity_persons=capacity,
                zone_type=ztype,
            )
            session.add(zone)
        zones[code] = zone
    await session.flush()
    return zones


async def seed_gates(session: AsyncSession, zones: dict[str, Zone]) -> int:
    created = 0
    for code, name, name_mr, zone_code, restricted, throughput, dlon, dlat in GATES:
        if await session.scalar(select(Gate).where(Gate.code == code)):
            continue
        session.add(
            Gate(
                code=code,
                name=name,
                name_mr=name_mr,
                zone_id=zones[zone_code].id,
                location=f"SRID=4326;POINT({TEMPLE_LON + dlon} {TEMPLE_LAT + dlat})",
                throughput_per_hour=throughput,
                is_restricted=restricted,
                is_open=not restricted,
            )
        )
        created += 1
    await session.flush()
    return created


async def seed_cameras(session: AsyncSession, zones: dict[str, Zone]) -> int:
    created = 0
    for name, zone_code, tripwire in CAMERAS:
        if await session.scalar(select(Camera).where(Camera.name == name)):
            continue
        session.add(
            Camera(
                zone_id=zones[zone_code].id,
                name=name,
                stream_url=None,  # simulation mode until a real RTSP feed exists
                status="offline",
                is_tripwire_enabled=tripwire,
            )
        )
        created += 1
    await session.flush()
    return created


# A plausible 1920x1080 view down a corridor: the near edge fills the bottom of
# the frame, the far edge is a narrower band higher up.  Real deployments click
# these four points on an actual still; this exists so a fresh clone starts
# calibrated rather than reporting densities it has not earned.
DEMO_FRAME = (1920, 1080)
DEMO_IMAGE_POINTS = [(200.0, 1000.0), (1720.0, 1000.0), (1180.0, 380.0), (740.0, 380.0)]


def _demo_world_points(area_m2: float) -> list[tuple[float, float]]:
    """A ground rectangle of the zone's own area, near edge twice the depth."""
    depth = max(4.0, (area_m2 / 2.0) ** 0.5)
    width = area_m2 / depth
    return [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]


async def seed_calibration(session: AsyncSession, zones: dict[str, Zone]) -> int:
    """Give every camera a homography.

    Section 4/M2 is blunt about this: without a homography the density number is
    fiction.  A demo that ships uncalibrated cameras is a demo that shows made-up
    numbers with a straight face, so the seed refuses to do that.
    """
    by_id = {zone.id: zone for zone in zones.values()}
    calibrated = 0
    rows = await session.execute(select(Camera))
    for camera in rows.scalars():
        if camera.homography_matrix is not None:
            continue
        zone = by_id.get(camera.zone_id)
        if zone is None:
            continue

        world = _demo_world_points(zone.area_m2)
        homography = solve_homography(DEMO_IMAGE_POINTS, world)
        camera.homography_matrix = {
            **homography.to_json(),
            "frame_width": DEMO_FRAME[0],
            "frame_height": DEMO_FRAME[1],
            "image_points": [list(p) for p in DEMO_IMAGE_POINTS],
            "world_points": [list(p) for p in world],
            "computed_zone_area_m2": zone.area_m2,
            "note": "development seed — replace with a real four-point calibration before deployment",
        }
        camera.calibrated_at = now_utc()
        calibrated += 1

    await session.flush()
    return calibrated


async def seed_facilities(session: AsyncSession, zones: dict[str, Zone]) -> int:
    created = 0
    for ftype, name, name_mr, zone_code, dlon, dlat in FACILITIES:
        if await session.scalar(select(Facility).where(Facility.name == name)):
            continue
        session.add(
            Facility(
                zone_id=zones[zone_code].id,
                type=ftype,
                name=name,
                name_mr=name_mr,
                location=f"SRID=4326;POINT({TEMPLE_LON + dlon} {TEMPLE_LAT + dlat})",
                status="operational",
            )
        )
        created += 1
    await session.flush()
    return created


async def seed_responders(session: AsyncSession) -> int:
    """Units on the board, so dispatch has something to suggest.

    Every unit is seeded with a position and a fresh `last_ping_at`. A roster of
    units with no known location would still rank — `dispatch_service.suggest`
    keeps them and flags the gap — but it would demonstrate the degraded case
    rather than the ordinary one on a first run.
    """
    created = 0
    for call_sign, unit_type, dlon, dlat in RESPONDERS:
        if await session.scalar(select(Responder).where(Responder.call_sign == call_sign)):
            continue
        session.add(
            Responder(
                call_sign=call_sign,
                unit_type=unit_type,
                status="available",
                current_location=f"SRID=4326;POINT({TEMPLE_LON + dlon} {TEMPLE_LAT + dlat})",
                last_ping_at=now_utc(),
            )
        )
        created += 1
    await session.flush()
    return created


# --- Palkhi route (Section 4/M8, Phase 9) ----------------------------------
# The real Sant Dnyaneshwar Maharaj Palkhi road, Alandi to Pandharpur: about
# 250 km over 18 days. The halt towns and their order are the actual ones; the
# coordinates are town centres rather than surveyed camp polygons, which is
# what `halt_towns.geom` is for once a district has digitised them.
#
# Seeded because the halt-town readiness board is unreadable with no route
# behind it — an empty board and a working board look identical, and the first
# thing anybody does with this module is open it.
WARI_ROUTE = ("Sant Dnyaneshwar Maharaj Palkhi", "संत ज्ञानेश्वर महाराज पालखी", "Alandi", 250.0)

#: (name, name_mr, lon, lat, day_offset) in walking order. Day offsets are the
#: traditional schedule; the year's actual dates move with Ashadhi Ekadashi.
HALT_TOWNS = [
    ("Alandi", "आळंदी", 73.8977, 18.6773, 0),
    ("Pune", "पुणे", 73.8567, 18.5204, 2),
    ("Saswad", "सासवड", 74.0333, 18.3500, 4),
    ("Jejuri", "जेजुरी", 74.1600, 18.2769, 5),
    ("Valha", "वाल्हे", 74.1833, 18.1667, 6),
    ("Lonand", "लोणंद", 74.1833, 17.9833, 7),
    ("Taradgaon", "तरडगाव", 74.2333, 17.8833, 8),
    ("Phaltan", "फलटण", 74.4333, 17.9833, 9),
    ("Barad", "बरड", 74.6000, 17.8000, 10),
    ("Natepute", "नातेपुते", 74.8000, 17.9000, 11),
    ("Malshiras", "माळशिरस", 74.9167, 17.8500, 12),
    ("Velapur", "वेळापूर", 75.0500, 17.8000, 13),
    ("Bhandishegaon", "भंडीशेगाव", 75.1500, 17.7500, 14),
    ("Wakhari", "वाखरी", 75.2833, 17.7000, 15),
    ("Pandharpur", "पंढरपूर", 75.3306, 17.6797, 16),
]

#: (code, name, name_mr, leader, phone, expected_count) — four Dindis, sized
#: the way real ones are: a few hundred walkers each, not thousands.
DINDIS = [
    ("DND-001", "Sant Tukaram Dindi", "संत तुकाराम दिंडी", "Ramesh Pawar", "9822010001", 420),
    ("DND-002", "Vitthal Bhakti Dindi", "विठ्ठल भक्ती दिंडी", "Sunita Jadhav", "9822010002", 260),
    ("DND-003", "Jnaneshwar Mauli Dindi", "ज्ञानेश्वर माऊली दिंडी", "Anil Deshmukh", "9822010003", 810),
    ("DND-004", "Rukmini Dindi", "रुक्मिणी दिंडी", "Kavita Shinde", "9822010004", 150),
]


async def seed_wari_route(session: AsyncSession) -> tuple[int, int, int]:
    """Route, halt towns and Dindis with a schedule (Section 4/M8).

    Halt towns are seeded with *deliberately uneven* provisioning: some ready,
    some short, one claiming ready without the water to back it. The readiness
    board's most important feature is the disagreement between what a town
    declared and what its numbers support, and a seed where every town is
    perfectly stocked demonstrates the boring case.
    """
    from datetime import timedelta

    from app.models import Dindi, DindiScheduleStop, HaltTown, Route
    from app.services.palkhi_service import store_leader_contact

    name, name_mr, origin, total_km = WARI_ROUTE
    route = await session.scalar(select(Route).where(Route.name == name))
    if route is None:
        path = ", ".join(f"{lon} {lat}" for _n, _m, lon, lat, _d in HALT_TOWNS)
        route = Route(
            name=name,
            name_mr=name_mr,
            origin=origin,
            path=f"SRID=4326;LINESTRING({path})",
            total_km=total_km,
            year=2026,
        )
        session.add(route)
        await session.flush()

    # Day 0 is the departure from Alandi. Anchored to today so the board has
    # something in its 36-hour window on a first run rather than a route whose
    # every arrival is in the past.
    day_zero = now_utc().replace(hour=6, minute=0, second=0, microsecond=0) - timedelta(days=4)

    towns: list[HaltTown] = []
    created_towns = 0
    for index, (town_name, town_mr, lon, lat, day) in enumerate(HALT_TOWNS, start=1):
        town = await session.scalar(select(HaltTown).where(HaltTown.name == town_name))
        if town is None:
            # Uneven on purpose — see the docstring. Every fourth town is short
            # of water, and every fifth declares itself ready anyway.
            water = 0 if index % 4 == 0 else 6
            declared = "ready" if index % 5 == 0 else ("partial" if water else "not_ready")
            town = HaltTown(
                name=town_name,
                name_mr=town_mr,
                route_id=route.id,
                sequence=index,
                centroid=f"SRID=4326;POINT({lon} {lat})",
                expected_arrival=day_zero + timedelta(days=day, hours=12),
                water_points=water,
                sanitation_units=0 if index % 4 == 0 else 14,
                medical_camps=0 if index % 4 == 0 else 1,
                readiness_status=declared,
                readiness_note="development seed — replace with a surveyed count",
            )
            session.add(town)
            created_towns += 1
        towns.append(town)
    await session.flush()

    created_dindis = 0
    created_stops = 0
    for offset, (code, dname, dname_mr, leader, phone, count) in enumerate(DINDIS):
        dindi = await session.scalar(select(Dindi).where(Dindi.code == code))
        if dindi is not None:
            continue
        dindi = Dindi(
            code=code,
            name=dname,
            name_mr=dname_mr,
            leader_name=leader,
            leader_phone_hash=await store_leader_contact(session, phone, at=now_utc()),
            expected_count=count,
            route_id=route.id,
            status="walking",
            started_at=day_zero,
            is_active=True,
        )
        session.add(dindi)
        await session.flush()
        created_dindis += 1

        # Each Dindi walks the same road a couple of hours apart, which is how
        # the Wari actually moves and what makes a halt town's head count the
        # sum of several groups rather than one.
        for index, (town, (_n, _m, _lon, _lat, day)) in enumerate(zip(towns, HALT_TOWNS, strict=True), start=1):
            session.add(
                DindiScheduleStop(
                    dindi_id=dindi.id,
                    halt_town_id=town.id,
                    sequence=index,
                    planned_arrival=day_zero + timedelta(days=day, hours=12 + offset * 2),
                    planned_departure=day_zero + timedelta(days=day + 1, hours=6 + offset * 2),
                    expected_count=count,
                )
            )
            created_stops += 1

    await session.flush()
    return created_towns, created_dindis, created_stops


async def main() -> None:
    async with SessionFactory() as session:
        users = await seed_users(session)
        zones = await seed_zones(session)
        gates = await seed_gates(session, zones)
        cameras = await seed_cameras(session, zones)
        calibrated = await seed_calibration(session, zones)
        facilities = await seed_facilities(session, zones)
        responders = await seed_responders(session)
        halt_towns, dindis, stops = await seed_wari_route(session)
        await session.commit()

    print("WariVerse development seed complete")
    print(f"  users      +{users}")
    # Not a "+" count: `seed_zones` returns every zone it resolved, created or
    # found. Printing it as `+6` next to the genuine creation counts made a
    # fully-seeded database look like it had just made six new zones.
    print(f"  zones       {len(zones)} present")
    print(f"  gates      +{gates}")
    print(f"  cameras    +{cameras}  ({calibrated} calibrated)")
    print(f"  facilities +{facilities}")
    print(f"  responders +{responders}")
    print(f"  halt towns +{halt_towns}  dindis +{dindis}  schedule stops +{stops}")
    print()
    print("Staff sign-in (POST /api/v1/auth/login):")
    for phone, name, role in STAFF:
        print(f"  {phone}  {role:<16}  {name}")
    print(f"  password: {DEMO_PASSWORD}")
    print()
    print("Administrator and System Admin also need MFA enrolment:")
    print("  POST /api/v1/auth/mfa/enrol  ->  scan the URI  ->  /auth/mfa/enrol/confirm")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
