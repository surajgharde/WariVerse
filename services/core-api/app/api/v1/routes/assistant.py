"""The pilgrim assistant (Section 13, Phase 9).

    POST /assistant/ask        GET /assistant/turns   (review, audited)

Two decisions about this endpoint are worth stating.

**It requires sign-in**, unlike `/facilities` and `/pilgrim/essentials` which a
pilgrim reaches anonymously.  Not for gatekeeping: `get_pass_status` has to be
scoped to *this caller's* passes, and an anonymous endpoint has no caller to
scope to.  A pilgrim who has booked a pass has already signed in with an OTP, so
the cost is nil for the person the tool is for. Signing in also means an
assistant that fronts a paid model cannot be drained by the open internet.

**Answers come back in two fields.**  `answer` is the reply; `answer_mr` is a
Marathi version, populated only when the answer came from a template — a triage
redirect, a refusal, or the deterministic fallback. When the model answers it
replies in the pilgrim's own language and `answer_mr` is null, because a second
round-trip to translate the assistant's own words is latency spent introducing
errors. The client renders `answer_mr or answer`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import AssistantTurn
from app.schemas.common import ApiModel, ErrorResponse, Page
from app.services import audit_service
from app.services.assistant import service as assistant_service
from app.services.assistant import tools
from app.services.audit_service import AuditAction

router = APIRouter(tags=["assistant"], responses={404: {"model": ErrorResponse}})


class AskIn(ApiModel):
    question: str = Field(min_length=1, max_length=1000)
    #: Groups the turns of one conversation.  Client-generated and opaque — not
    #: a user id, and deliberately not derived from one.
    session_id: str = Field(min_length=8, max_length=64)
    language: str = Field(default="mr", max_length=8)
    #: Position, when the app has it.  Optional: a phone with location switched
    #: off still gets facility answers, just not ordered by distance.
    lon: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)


class ToolCallOut(ApiModel):
    """What the answer was built from.

    Served to the client, not just logged. Section 0 rule 3 — no AI output is
    presented as certainty — and the honest way to apply it to a chat answer is
    to let the interface show that this sentence rests on a `get_pass_status`
    that found something, or on nothing at all.
    """

    name: str
    found: bool
    summary: str
    ms: int = 0


class AskOut(ApiModel):
    answer: str
    #: Marathi rendering, present only for templated answers. Render
    #: `answer_mr or answer`.
    answer_mr: str | None = None
    outcome: str
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    #: Set when a guardrail fired. `medical_emergency` is not a refusal to help
    #: — it is the emergency redirect, and the UI should render it with the SOS
    #: button rather than as a chat bubble.
    refusal_reason: str | None = None
    #: True when the model was unreachable and the deterministic path answered.
    #: The UI says so, rather than letting a terse templated reply read as
    #: rudeness.
    is_degraded: bool = False
    helpline: str
    turn_id: uuid.UUID
    answered_at: datetime
    notice: str
    notice_mr: str


_NOTICE = (
    "This assistant only repeats what the temple system knows. It cannot book or change a pass, "
    "and it cannot send help. For an emergency, call 108 or press SOS."
)
_NOTICE_MR = (
    "हा सहाय्यक फक्त मंदिर प्रणालीकडे असलेली माहिती सांगतो. तो पास बुक करू शकत नाही किंवा बदलू शकत "
    "नाही, आणि मदत पाठवू शकत नाही. आपत्कालीन परिस्थितीत १०८ वर फोन करा किंवा SOS दाबा."
)


@router.post("/assistant/ask", response_model=AskOut)
async def ask(
    body: AskIn,
    actor: Actor = Depends(require(Permission.ASSISTANT_USE)),
    session: AsyncSession = Depends(get_session),
) -> AskOut:
    """Ask the assistant one question.

    The caller's `phone_hash` is what scopes `get_pass_status` to their own
    passes — it is taken from the token here and never from the request body, so
    a question cannot ask about somebody else's pass no matter how it is worded.
    """
    result, turn = await assistant_service.ask(
        session,
        question=body.question,
        session_id=body.session_id,
        actor_id=actor.id,
        phone_hash=actor.user.phone_hash,
        language=body.language,
        lon=body.lon,
        lat=body.lat,
    )
    await session.commit()

    return AskOut(
        answer=result.answer,
        answer_mr=result.answer_mr,
        outcome=str(result.outcome),
        tool_calls=[ToolCallOut(**call) for call in result.tool_calls],
        refusal_reason=result.refusal_reason,
        is_degraded=result.is_degraded,
        helpline=result.helpline,
        turn_id=turn.id,
        answered_at=turn.created_at,
        notice=_NOTICE,
        notice_mr=_NOTICE_MR,
    )


class TurnOut(ApiModel):
    id: uuid.UUID
    session_id: str
    language: str
    question: str
    answer: str | None = None
    outcome: str
    refusal_reason: str | None = None
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    model: str | None = None
    latency_ms: int | None = None
    created_at: datetime


class TurnPage(Page[TurnOut]):
    #: Counts by outcome across the whole filtered set, not just this page.
    #: `no_data` rising is an outage; `refused` rising is a product telling
    #: users the wrong thing about what the assistant is for. A reviewer needs
    #: to be able to tell those apart at a glance.
    outcomes: dict[str, int] = Field(default_factory=dict)
    tools_available: list[str] = Field(default_factory=list)


@router.get("/assistant/turns", response_model=TurnPage)
async def list_turns(
    outcome: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require(Permission.ASSISTANT_REVIEW)),
    session: AsyncSession = Depends(get_session),
) -> TurnPage:
    """The transcript, for review (Section 13's last line).

    Reading it is itself audited. These rows are pilgrims' questions — redacted
    of contact numbers, but still what somebody typed while frightened or lost —
    and a log of who read them is the least this owes them.

    `tools_available` is served alongside because the reviewer's real question
    is usually "could it have known that", and the answer is the tool list.
    """
    filters = []
    if outcome:
        filters.append(AssistantTurn.outcome == outcome)
    if session_id:
        filters.append(AssistantTurn.session_id == session_id)

    total = await session.scalar(
        select(func.count()).select_from(AssistantTurn).where(*filters)
    ) or 0

    rows = (
        await session.execute(
            select(AssistantTurn)
            .where(*filters)
            .order_by(AssistantTurn.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()

    counts = await session.execute(
        select(AssistantTurn.outcome, func.count()).where(*filters).group_by(AssistantTurn.outcome)
    )

    await audit_service.record(
        session,
        action=AuditAction.AUDIT_VIEWED,
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="assistant_turns",
        meta={"outcome": outcome, "session_id": session_id, "limit": limit, "offset": offset},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return TurnPage(
        items=[
            TurnOut(
                id=t.id,
                session_id=t.session_id,
                language=t.language,
                question=t.question,
                answer=t.answer,
                outcome=t.outcome,
                refusal_reason=t.refusal_reason,
                tool_calls=[ToolCallOut(**c) for c in t.tool_calls],
                model=t.model,
                latency_ms=t.latency_ms,
                created_at=t.created_at,
            )
            for t in rows
        ],
        total=int(total),
        limit=limit,
        offset=offset,
        outcomes={name: int(count) for name, count in counts},
        tools_available=sorted(tools.TOOL_NAMES),
    )


@router.get("/assistant/capabilities")
async def capabilities(
    _: Actor = Depends(require(Permission.ASSISTANT_USE)),
) -> dict[str, object]:
    """What the assistant can and cannot do, served rather than hardcoded in the app.

    Section 13 draws a narrow contract, and a pilgrim app that describes that
    contract from a string baked into a build will describe last month's
    contract after the next change. The onboarding card reads from here.
    """
    return {
        "can": [
            {"en": "Darshan and aarti timings", "mr": "दर्शन आणि आरतीच्या वेळा"},
            {"en": "Your own pass and the current wait estimate", "mr": "तुमचा पास आणि सध्याचा प्रतीक्षा अंदाज"},
            {"en": "How crowded an area is right now", "mr": "एखाद्या भागात सध्या किती गर्दी आहे"},
            {
                "en": "Nearest water, toilet, medical camp or help desk",
                "mr": "जवळचे पाणी, स्वच्छतागृह, वैद्यकीय शिबिर किंवा मदत कक्ष",
            },
            {
                "en": "Help writing an emergency report for you to send",
                "mr": "तुम्ही पाठवण्यासाठी आपत्कालीन अहवाल लिहिण्यात मदत",
            },
        ],
        "cannot": [
            {"en": "Book, change or cancel a pass", "mr": "पास बुक करणे, बदलणे किंवा रद्द करणे"},
            {"en": "Send an ambulance or any team", "mr": "रुग्णवाहिका किंवा कोणतेही पथक पाठवणे"},
            {
                "en": "Open or close a gate, or change a crowd setting",
                "mr": "द्वार उघडणे-बंद करणे, किंवा गर्दीविषयक सेटिंग बदलणे",
            },
            {"en": "Judge how serious an illness or injury is", "mr": "आजार किंवा दुखापत किती गंभीर आहे हे ठरवणे"},
        ],
        "emergency_number": "108",
        "tools": sorted(tools.TOOL_NAMES),
        "generated_at": now_utc(),
    }
