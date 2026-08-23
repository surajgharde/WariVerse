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

from app.core.db import SessionFactory, dispose_engine
from app.core.permissions import Role
from app.core.security import hash_password, hash_phone, normalise_phone
from app.models import Camera, Facility, Gate, User, Zone

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
    for phone, name, role in STAFF:
        phone_hash = hash_phone(phone)
        if await session.scalar(select(User).where(User.phone_hash == phone_hash)):
            continue
        session.add(
            User(
                phone=normalise_phone(phone),
                phone_hash=phone_hash,
                name=name,
                role=role,
                language="mr",
                password_hash=hash_password(DEMO_PASSWORD),
                is_active=True,
            )
        )
        created += 1
    await session.flush()
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


async def main() -> None:
    async with SessionFactory() as session:
        users = await seed_users(session)
        zones = await seed_zones(session)
        gates = await seed_gates(session, zones)
        cameras = await seed_cameras(session, zones)
        facilities = await seed_facilities(session, zones)
        await session.commit()

    print("WariVerse development seed complete")
    print(f"  users      +{users}")
    print(f"  zones      +{len(zones)}")
    print(f"  gates      +{gates}")
    print(f"  cameras    +{cameras}")
    print(f"  facilities +{facilities}")
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
