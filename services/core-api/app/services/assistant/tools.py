"""The assistant's fixed tool list (Section 13).

    get_pass_status   get_zone_crowd   find_nearest_facility
    get_schedule      raise_sos_draft

Five functions.  Not five plus whatever seems useful later — the list is fixed,
and the fixedness is the control.  Section 13's MAY NOT DO list is enforced here
by *absence*: there is no `cancel_pass`, no `set_threshold`, no `dispatch_unit`,
so no prompt, injection or model error can reach one.  A guardrail the model
could talk its way past is not a guardrail.

Three properties every tool in this file has:

1. **It reads.**  The one tool with a verb in its name, `raise_sos_draft`,
   drafts and returns; it does not create an incident.  Section 13 named it a
   *draft* and the name is load-bearing — a language model must not be able to
   raise an alarm in a control room, and it equally must not be the thing that
   decides an alarm is unnecessary.
2. **It reports absence honestly.**  Every result carries `found`.  A tool that
   returned `{}` for "no such pass" and `{}` for "database down" would let the
   model narrate the same sentence for both.
3. **It carries provenance.**  Anything measured comes back with `observed_at`
   and `is_stale`, because the answer built from it has to say how old it is.
   Section 4/M7: never render stale crowd data as if it were live.

The pilgrim boundary is respected exactly as `/crowd/public` respects it. The
assistant talks to pilgrims, so `get_zone_crowd` returns a band and the standard
advice sentence — never a head count, a density figure or a flow vector. Those
would tell anyone with a phone where the crowd is thickest, and the assistant is
not a way around Section 12.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import now_utc
from app.models import Facility, Pass, PassStatus, Zone
from app.services import config_service, crowd_service, pass_service

#: Tool names, referenced by the model and stored on every turn.  Keep stable:
#: a review query against six months of transcripts joins on these strings.
GET_PASS_STATUS = "get_pass_status"
GET_ZONE_CROWD = "get_zone_crowd"
FIND_NEAREST_FACILITY = "find_nearest_facility"
GET_SCHEDULE = "get_schedule"
RAISE_SOS_DRAFT = "raise_sos_draft"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool is allowed to know about who is asking.

    `phone_hash` is what scopes `get_pass_status` to the caller's own passes.
    It is an HMAC, not a number — the assistant cannot look up a stranger's pass
    because it never holds anything it could look one up with.
    """

    session: AsyncSession
    phone_hash: str | None = None
    actor_id: uuid.UUID | None = None
    language: str = "mr"
    #: The pilgrim's position, when the app has offered it. Optional: a phone
    #: with location switched off still gets facility answers, just unordered.
    lon: float | None = None
    lat: float | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    name: str
    found: bool
    data: dict[str, Any]
    #: What goes into the transcript. Never the payload — copying a
    #: `get_pass_status` result here would put a pass reference and slot time
    #: into a table with a longer retention than the pass itself.
    summary: str

    def as_log(self, ms: int) -> dict[str, Any]:
        return {"name": self.name, "found": self.found, "summary": self.summary, "ms": ms}


# ---------------------------------------------------------------------------
# tool declarations, in Gemini's function-calling schema
# ---------------------------------------------------------------------------
#: The descriptions are written for the model, and they carry the constraints
#: rather than leaving them to the system prompt. A model choosing between five
#: tools reads these far more reliably than it re-reads a preamble.
DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": GET_PASS_STATUS,
        "description": (
            "Look up the caller's own darshan pass: its slot time, gate, status and the current "
            "wait estimate. Only the caller's passes are visible. Call this for any question about "
            "'my pass', 'my slot', 'when is my darshan' or 'how long is the wait'. "
            "Never state a wait time that did not come from this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Pass reference like WV3F8K2Q1M. Omit to get the caller's most recent pass.",
                }
            },
        },
    },
    {
        "name": GET_ZONE_CROWD,
        "description": (
            "How crowded an area of the temple complex is right now, as a band (safe, moderate, "
            "high, critical) with the official advice sentence for that band. Returns no head "
            "count and no density number — those are not public. If the reading is missing or "
            "stale the tool says so, and you must tell the pilgrim the area is UNKNOWN rather "
            "than clear."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "zone": {
                    "type": "string",
                    "description": "Zone code or part of its name. Omit for every zone.",
                }
            },
        },
    },
    {
        "name": FIND_NEAREST_FACILITY,
        "description": (
            "Find toilets, drinking water, medical camps, food, rest zones, lost-and-found or "
            "help desks, ordered by distance when the caller's position is known. Returns "
            "locations and working status only — never how busy a facility is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "One of: toilet, water, medical, food, rest_zone, lost_and_found, "
                        "help_desk, charging."
                    ),
                },
                "limit": {"type": "integer", "description": "How many to return, 1-10. Default 3."},
            },
            "required": ["type"],
        },
    },
    {
        "name": GET_SCHEDULE,
        "description": (
            "Darshan opening and closing times and the daily aarti timings. Use for any question "
            "about when something happens at the temple. These are scheduled times, not "
            "predictions — do not add a wait estimate to them."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": RAISE_SOS_DRAFT,
        "description": (
            "Prepare — but do NOT send — an emergency report for the pilgrim to review and submit "
            "themselves. Use only when the pilgrim describes a situation needing help and asks you "
            "to report it. This tool never contacts anyone. Always tell the pilgrim that nothing "
            "has been sent yet, give them the ambulance number 108, and tell them to press the SOS "
            "button in the app to actually send it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "medical, missing_person, crowd_crush, fire, other",
                },
                "description": {
                    "type": "string",
                    "description": "What the pilgrim described, in their own words, one or two sentences.",
                },
            },
            "required": ["type", "description"],
        },
    },
]

TOOL_NAMES = frozenset(d["name"] for d in DECLARATIONS)


# ---------------------------------------------------------------------------
# implementations
# ---------------------------------------------------------------------------
async def get_pass_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The caller's own pass, with the wait estimate the pass screen shows.

    Scoped to `ctx.phone_hash` on the query itself, not filtered afterwards.
    A reference belonging to somebody else simply does not match, so the
    assistant cannot be argued into looking one up — there is nothing to argue
    with, only a WHERE clause.
    """
    if ctx.phone_hash is None:
        return ToolResult(
            GET_PASS_STATUS,
            found=False,
            data={"reason": "not_signed_in"},
            summary="caller not signed in",
        )

    reference = (args.get("reference") or "").strip().upper() or None
    stmt = select(Pass).where(Pass.holder_phone_hash == ctx.phone_hash)
    if reference:
        stmt = stmt.where(Pass.reference == reference)
    stmt = stmt.order_by(Pass.issued_at.desc()).limit(1)

    record = await ctx.session.scalar(stmt)
    if record is None:
        return ToolResult(
            GET_PASS_STATUS,
            found=False,
            data={"reason": "no_pass_for_caller", "reference": reference},
            summary=f"no pass found (ref={reference or 'latest'})",
        )

    view = await pass_service.describe_pass(ctx.session, record)
    start, end = pass_service.slot_bounds(view.slot)
    return ToolResult(
        GET_PASS_STATUS,
        found=True,
        data={
            "reference": record.reference,
            "status": str(record.status),
            "group_size": record.group_size,
            "slot_date": view.slot.date.isoformat(),
            "slot_start": start.isoformat(),
            "slot_end": end.isoformat(),
            "gate": view.gate_code,
            "people_ahead": view.queue_ahead,
            # The one number Section 13 singles out. It comes from
            # `slot_service.estimate_wait`, the same function the pass screen
            # uses, so the assistant and the app cannot disagree about it.
            "estimated_entry_at": view.eta.isoformat(),
            "was_reslotted": view.is_reslotted,
            "is_active": record.status == PassStatus.ACTIVE,
        },
        summary=f"pass {record.reference} {record.status}",
    )


async def get_zone_crowd(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Crowd bands for the pilgrim-facing view.

    Deliberately identical in content to `GET /crowd/public`: band, official
    advice, age. If the assistant could see more than the public endpoint, it
    would be a way around Section 12 that happens to speak Marathi.
    """
    wanted = (args.get("zone") or "").strip().lower()
    zones = await crowd_service.load_zones(ctx.session)
    snapshots = {s.zone_id: s for s in await crowd_service.latest(ctx.session)}

    rows: list[dict[str, Any]] = []
    for zone_id, zone in sorted(zones.items(), key=lambda kv: kv[1].code):
        # Matched against code, English name and Marathi name. A pilgrim asking
        # in Marathi about "पूर्व मार्गिका" must reach the same zone as an
        # operator typing "EAST-COR", or the assistant is an English product
        # with Marathi output.
        haystack = f"{zone.code} {zone.name} {zone.name_mr}".lower()
        if wanted and wanted not in haystack:
            continue
        snap = snapshots.get(zone_id)
        if snap is None or snap.is_stale:
            advice, advice_mr = crowd_service.UNKNOWN_ADVICE
            rows.append(
                {
                    "zone": zone.code,
                    "name": zone.name,
                    "name_mr": zone.name_mr,
                    "level": None,
                    "advice": advice,
                    "advice_mr": advice_mr,
                    "observed_at": snap.observed_at.isoformat() if snap else None,
                    "is_stale": True,
                }
            )
            continue
        advice, advice_mr = crowd_service.PUBLIC_ADVICE[snap.level]
        rows.append(
            {
                "zone": zone.code,
                "name": zone.name,
                "name_mr": zone.name_mr,
                "level": str(snap.level),
                "advice": advice,
                "advice_mr": advice_mr,
                "observed_at": snap.observed_at.isoformat(),
                "age_seconds": round(snap.age_seconds),
                "is_stale": False,
            }
        )

    live = [r for r in rows if not r["is_stale"]]
    return ToolResult(
        GET_ZONE_CROWD,
        found=bool(live),
        data={
            "zones": rows,
            "notice": (
                "Levels are estimates from anonymous counting. An area with no reading is UNKNOWN, "
                "not clear."
            ),
        },
        summary=f"{len(live)} live of {len(rows)} zone(s)" if rows else "no matching zone",
    )


#: The facility types the seed data and Section 4/M7 use.  An unknown type
#: comes back as a miss with the list attached, so the model can retry with a
#: real one rather than telling the pilgrim there are no toilets.
FACILITY_TYPES = (
    "toilet",
    "water",
    "medical",
    "food",
    "rest_zone",
    "lost_and_found",
    "help_desk",
    "charging",
)


async def find_nearest_facility(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Facilities of a type, nearest first when the caller's position is known.

    Out-of-service facilities are returned with their status rather than hidden.
    "The water point on my map is broken" is worth knowing before walking to it;
    a silently missing pin just looks like the map is wrong.
    """
    ftype = (args.get("type") or "").strip().lower()
    if ftype not in FACILITY_TYPES:
        return ToolResult(
            FIND_NEAREST_FACILITY,
            found=False,
            data={"reason": "unknown_type", "valid_types": list(FACILITY_TYPES)},
            summary=f"unknown facility type '{ftype}'",
        )

    limit = max(1, min(10, int(args.get("limit") or 3)))
    has_position = ctx.lon is not None and ctx.lat is not None

    # Distance is selected as a real column rather than computed in Python so
    # PostGIS does the ordering on the indexed geography — and so "nearest"
    # means nearest on the ground, not nearest in degrees of longitude, which
    # at 17.68°N is a 5% error in the wrong direction.
    if has_position:
        here = func.ST_SetSRID(func.ST_MakePoint(ctx.lon, ctx.lat), 4326)
        distance = func.ST_Distance(cast(Facility.location, Geography), cast(here, Geography))
    else:
        distance = literal(None)

    stmt = (
        select(
            Facility.name,
            Facility.name_mr,
            Facility.status,
            Facility.notes_mr,
            func.ST_X(Facility.location),
            func.ST_Y(Facility.location),
            Zone.name,
            Zone.name_mr,
            distance,
        )
        .outerjoin(Zone, Zone.id == Facility.zone_id)
        .where(Facility.type == ftype)
        .order_by(distance if has_position else Facility.name)
        .limit(limit)
    )

    items = [
        {
            "name": name,
            "name_mr": name_mr,
            "status": status,
            "note_mr": note_mr,
            "location": [float(lon), float(lat)],
            "zone": zone_name,
            "zone_mr": zone_name_mr,
            **({"metres_away": round(float(metres))} if metres is not None else {}),
        }
        for name, name_mr, status, note_mr, lon, lat, zone_name, zone_name_mr, metres in (
            await ctx.session.execute(stmt)
        )
    ]
    return ToolResult(
        FIND_NEAREST_FACILITY,
        found=bool(items),
        data={
            "type": ftype,
            "facilities": items,
            "ordered_by_distance": has_position,
            "notice": "This list does not say how busy a facility is.",
        },
        summary=f"{len(items)} {ftype}(s)" + ("" if has_position else ", position unknown"),
    )


async def get_schedule(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    """Darshan window and daily aarti timings.

    Derived from the same `system_config` the slot grid is built from, so the
    assistant cannot tell somebody the temple opens at a time no slot exists
    for. The aarti timings are the fixed daily ritual schedule.
    """
    day_start = str(await config_service.get(ctx.session, "day_start"))
    day_end = str(await config_service.get(ctx.session, "day_end"))
    return ToolResult(
        GET_SCHEDULE,
        found=True,
        data={
            "darshan_opens": day_start,
            "darshan_closes": day_end,
            "timezone": "Asia/Kolkata",
            "aarti": [
                {"name": "Kakada aarti", "name_mr": "काकडा आरती", "time": "04:30"},
                {"name": "Madhyan aarti", "name_mr": "माध्यान्ह आरती", "time": "11:30"},
                {"name": "Dhoop aarti", "name_mr": "धूप आरती", "time": "19:00"},
                {"name": "Shej aarti", "name_mr": "शेज आरती", "time": "23:30"},
            ],
            "notice": "These are scheduled times, not predictions of when you will get darshan.",
        },
        summary=f"darshan {day_start}-{day_end}",
    )


async def raise_sos_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Prepare an emergency report.  **Sends nothing.**

    The name is the contract.  Section 13 lists `raise_sos_draft` among the
    tools and forbids the assistant from assessing a medical emergency, and
    those two only sit together one way: the model may help a frightened person
    put words in a box, and a human presses send.

    Two failure modes are being avoided, in opposite directions. A model that
    could raise a real SOS puts a language model in a control room's alarm
    queue. A model that refused to help at all leaves somebody composing an
    emergency report in their second language under the worst pressure of their
    life. Drafting — and saying loudly that nothing has been sent — is the only
    position that is not one of those two.
    """
    kind = (args.get("type") or "other").strip().lower()
    if kind not in {"medical", "missing_person", "crowd_crush", "fire", "other"}:
        kind = "other"

    return ToolResult(
        RAISE_SOS_DRAFT,
        found=True,
        data={
            "draft": {
                "type": kind,
                "description": (args.get("description") or "").strip()[:500],
            },
            "sent": False,
            "submit_endpoint": "POST /api/v1/sos",
            "call_now": "108",
            "control_room_sms": settings.control_room_sms_number or None,
            "instruction": (
                "NOTHING HAS BEEN SENT. Tell the pilgrim to call 108 now if anyone is hurt, and to "
                "press the SOS button in the app to send this report."
            ),
        },
        summary=f"drafted {kind} SOS (not sent)",
    )


TOOLS = {
    GET_PASS_STATUS: get_pass_status,
    GET_ZONE_CROWD: get_zone_crowd,
    FIND_NEAREST_FACILITY: find_nearest_facility,
    GET_SCHEDULE: get_schedule,
    RAISE_SOS_DRAFT: raise_sos_draft,
}


async def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[ToolResult, int]:
    """Dispatch one tool call, returning the result and how long it took.

    An unknown name is a miss, not an exception. Models hallucinate function
    names, and the right response is to hand back "that tool does not exist"
    so the next turn corrects itself — not to 500 a pilgrim's question.
    """
    started = now_utc()
    handler = TOOLS.get(name)
    if handler is None:
        return (
            ToolResult(
                name,
                found=False,
                data={"reason": "no_such_tool", "available": sorted(TOOL_NAMES)},
                summary=f"unknown tool '{name}'",
            ),
            0,
        )
    try:
        result = await handler(ctx, args or {})
    except AppError as exc:
        result = ToolResult(
            name, found=False, data={"reason": exc.code}, summary=f"{name} failed: {exc.code}"
        )
    except Exception as exc:
        result = ToolResult(
            name,
            found=False,
            data={"reason": "tool_error"},
            summary=f"{name} failed: {type(exc).__name__}",
        )
    return result, int((now_utc() - started).total_seconds() * 1000)
