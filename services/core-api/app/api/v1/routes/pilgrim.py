"""What the pilgrim app needs and nothing else (Section 4/M7, Phase 7).

    GET /facilities          GET /pilgrim/essentials

Two endpoints, both anonymous, both designed around one constraint: **the
service worker has to be able to cache the answer and still be honest with it
four hours later on a dead network.**

That constraint shapes what is in here and what is deliberately not.

* `/facilities` is where the toilets, water points and medical camps are. It is
  public because a pilgrim looking for a toilet at 2 a.m. should not have to
  sign in, and because the locations are painted on boards around the town
  already. It carries no live occupancy — a cached "not crowded" from four
  hours ago would send somebody across the complex for nothing.
* `/pilgrim/essentials` is the small bundle of facts that must survive going
  offline: emergency numbers, ritual timings, the control-room SMS number for
  when the app cannot reach the API at all. Every one of these is a thing a
  frightened person needs when the network is exactly what has failed.

What is *not* here: anything that goes stale dangerously. Live crowd density is
served by `/crowd/public`, which carries its own `observed_at` and an explicit
`is_stale`, and the app is required to render it with its age. Splitting that
away from the cacheable bundle is the whole point — Section 4/M7: "Never render
stale crowd data as if it were live. That is how people walk into a crush."
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.security import now_utc
from app.models import Facility, Zone
from app.schemas.common import ApiModel, ErrorResponse
from app.services import config_service

router = APIRouter(tags=["pilgrim"], responses={404: {"model": ErrorResponse}})


# ---------------------------------------------------------------------------
# facilities
# ---------------------------------------------------------------------------
class FacilityOut(ApiModel):
    id: uuid.UUID
    type: str
    name: str
    name_mr: str
    #: [lon, lat], GeoJSON order — the same as everywhere else in this API.
    location: tuple[float, float]
    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    status: str
    capacity: int | None = None
    notes_mr: str | None = None


class FacilityList(ApiModel):
    facilities: list[FacilityOut]
    generated_at: datetime
    #: How long a cached copy of this stays useful. Facilities move rarely; the
    #: app may hold this for a day without misleading anybody, which is exactly
    #: what makes it worth caching for the offline case.
    cache_seconds: int = 86_400
    notice: str
    notice_mr: str


_FACILITY_NOTICE = (
    "Locations are fixed points. This list does not say how busy a facility is — "
    "check with a volunteer if it matters."
)
_FACILITY_NOTICE_MR = (
    "ही ठिकाणे ठरलेली आहेत. येथे किती गर्दी आहे हे या यादीत नाही — गरज असल्यास "
    "स्वयंसेवकाला विचारा."
)


@router.get("/facilities", response_model=FacilityList)
async def list_facilities(
    facility_type: str | None = Query(default=None, alias="type"),
    session: AsyncSession = Depends(get_session),
) -> FacilityList:
    """Toilets, water, medical camps, food, rest zones, lost-and-found desks.

    Anonymous on purpose. A pilgrim looking for a toilet at 2 a.m. should not
    meet a sign-in screen, and these locations are already painted on boards
    around the town — withholding them would protect nothing and cost somebody
    a long walk.

    Out-of-service facilities are returned rather than filtered out, with their
    status. "The water point marked on my map is broken" is a fact worth
    knowing before walking to it; a silently missing pin just looks like the map
    is wrong.
    """
    stmt = (
        select(
            Facility.id,
            Facility.type,
            Facility.name,
            Facility.name_mr,
            func.ST_X(Facility.location),
            func.ST_Y(Facility.location),
            Facility.zone_id,
            Zone.code,
            Facility.status,
            Facility.capacity,
            Facility.notes_mr,
        )
        .outerjoin(Zone, Zone.id == Facility.zone_id)
        .order_by(Facility.type, Facility.name)
    )
    if facility_type:
        stmt = stmt.where(Facility.type == facility_type)

    rows = await session.execute(stmt)
    return FacilityList(
        facilities=[
            FacilityOut(
                id=fid,
                type=ftype,
                name=name,
                name_mr=name_mr,
                location=(float(lon), float(lat)),
                zone_id=zone_id,
                zone_code=zone_code,
                status=status,
                capacity=capacity,
                notes_mr=notes_mr,
            )
            for fid, ftype, name, name_mr, lon, lat, zone_id, zone_code, status, capacity, notes_mr in rows
        ],
        generated_at=now_utc(),
        notice=_FACILITY_NOTICE,
        notice_mr=_FACILITY_NOTICE_MR,
    )


# ---------------------------------------------------------------------------
# essentials
# ---------------------------------------------------------------------------
class EmergencyContact(ApiModel):
    label: str
    label_mr: str
    number: str
    #: True for the one a pilgrim should try first. Exactly one is primary, so
    #: the app can render it as the big button without deciding for itself.
    is_primary: bool = False


class RitualTiming(ApiModel):
    name: str
    name_mr: str
    #: "HH:MM" in temple local time. A string rather than a datetime because
    #: these recur daily and the app renders them as a schedule, not an instant.
    time: str
    note_mr: str | None = None


class PilgrimEssentials(ApiModel):
    """The small bundle that must survive going offline.

    Everything here is either constant or changes on the scale of days, which is
    what makes it safe to cache. Anything that goes stale dangerously — live
    density above all — is deliberately served elsewhere with its own timestamp.
    """

    emergency_contacts: list[EmergencyContact]
    ritual_timings: list[RitualTiming]
    #: Where an SOS goes by SMS when the app cannot reach the API at all
    #: (Section 4/M4). Null when no gateway is configured, and the app says so
    #: rather than showing an empty button.
    control_room_sms: str | None = None
    #: Reading older than this is stale everywhere in the product. Served so the
    #: app's "last updated" banner uses the same line the server does.
    stale_reading_seconds: int
    generated_at: datetime
    cache_seconds: int = 86_400
    offline_notice: str
    offline_notice_mr: str


#: Emergency numbers. Hardcoded rather than configurable, deliberately: an
#: operator who can edit these can also empty them, and a pilgrim app that shows
#: no ambulance number because a form was saved wrong is a failure with no
#: recovery path. 108 and 112 are national and do not change.
_EMERGENCY: list[EmergencyContact] = [
    EmergencyContact(
        label="Ambulance / medical emergency",
        label_mr="रुग्णवाहिका / वैद्यकीय आपत्कालीन",
        number="108",
        is_primary=True,
    ),
    EmergencyContact(label="Police", label_mr="पोलीस", number="112"),
    EmergencyContact(label="Fire", label_mr="अग्निशमन", number="101"),
    EmergencyContact(
        label="Women's helpline", label_mr="महिला मदत क्रमांक", number="1091"
    ),
    EmergencyContact(
        label="Child helpline", label_mr="बाल मदत क्रमांक", number="1098"
    ),
]

_OFFLINE_NOTICE = (
    "You are offline. Crowd information is from the time shown and may have changed. "
    "Emergency numbers and your pass still work."
)
_OFFLINE_NOTICE_MR = (
    "तुम्ही ऑफलाइन आहात. गर्दीची माहिती दाखवलेल्या वेळेची आहे आणि ती बदललेली असू शकते. "
    "आपत्कालीन क्रमांक आणि तुमचा पास अजूनही चालतो."
)


@router.get("/pilgrim/essentials", response_model=PilgrimEssentials)
async def pilgrim_essentials(
    session: AsyncSession = Depends(get_session),
) -> PilgrimEssentials:
    """Everything the app must still be able to show with no network.

    Anonymous, because the moment this matters most is the moment a token has
    expired and the network that would refresh it is gone.
    """
    day_start = str(await config_service.get(session, "day_start"))
    day_end = str(await config_service.get(session, "day_end"))

    # Timings are derived from the configured darshan window rather than typed
    # in separately, so the app and the slot grid cannot disagree about when the
    # temple opens.
    timings = [
        RitualTiming(
            name="Darshan opens",
            name_mr="दर्शन सुरू",
            time=day_start,
            note_mr="पहिल्या स्लॉटची वेळ",
        ),
        RitualTiming(
            name="Darshan closes",
            name_mr="दर्शन समाप्त",
            time=day_end,
            note_mr="शेवटच्या स्लॉटची वेळ",
        ),
        RitualTiming(name="Kakada aarti", name_mr="काकडा आरती", time="04:30"),
        RitualTiming(name="Madhyan aarti", name_mr="माध्यान्ह आरती", time="11:30"),
        RitualTiming(name="Dhoop aarti", name_mr="धूप आरती", time="19:00"),
        RitualTiming(name="Shej aarti", name_mr="शेज आरती", time="23:30"),
    ]

    return PilgrimEssentials(
        emergency_contacts=_EMERGENCY,
        ritual_timings=timings,
        control_room_sms=settings.control_room_sms_number or None,
        stale_reading_seconds=settings.stale_reading_seconds,
        generated_at=now_utc(),
        offline_notice=_OFFLINE_NOTICE,
        offline_notice_mr=_OFFLINE_NOTICE_MR,
    )
