"""One assistant turn, end to end (Section 13, Phase 9).

The order of operations is the design, so it is worth reading as a list before
reading it as code:

1. **Triage, before anything else.**  An emergency is redirected to 108 and the
   SOS button without a model being involved.  A request to cancel a pass or
   close a gate is refused with a pointer to who can actually do it.  Neither
   costs an API call, and neither depends on a model choosing correctly.
2. **Rate limit per session**, not per person.  A loop is what this guards
   against, and a frightened person asking the same question five times is not
   a loop.
3. **Ask the model**, with the five read-only tools.
4. **Fall back deterministically** on any failure — no key, timeout, HTTP
   error, empty response, model that never stopped calling tools.
5. **Log the turn** with its tool calls, always, including the failures.
   Section 13's last line, and the only thing that makes the rest auditable.

Step 5 happens even when steps 3 and 4 both went wrong.  A turn that errored is
exactly the turn a reviewer wants to find.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis_client import aw, redis
from app.core.security import now_utc
from app.models import AssistantTurn, TurnOutcome
from app.services import config_service
from app.services.assistant import fallback, gemini, guardrails, prompt, tools
from app.services.assistant.guardrails import TriageVerdict
from app.services.assistant.tools import ToolContext

logger = get_logger(__name__)

MAX_QUESTION_CHARS = 1000


@dataclass(slots=True)
class AssistantAnswer:
    """What the route returns and what the transcript stores.

    `answer` and `answer_mr` are both populated in the deterministic paths — a
    triage redirect, a refusal, the fallback — because those are templates and
    templates come in pairs. When the model answers, it replies in *one*
    language (the pilgrim's), so `answer_mr` is None and the client renders
    `answer` as-is. The alternative would be a second round-trip to translate
    the assistant's own words back into Marathi, which is a way to spend
    latency introducing errors.
    """

    answer: str
    answer_mr: str | None
    outcome: TurnOutcome
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    refusal_reason: str | None = None
    model: str | None = None
    latency_ms: int = 0
    #: True whenever the answer came from `fallback.py`. Served to the client so
    #: the UI can say the assistant is running without its model, rather than
    #: letting a terse templated reply read as rudeness.
    is_degraded: bool = False
    helpline: str = prompt.HELPLINE


async def _rate_limited(session_id: str, ceiling: int) -> bool:
    """A per-session hourly ceiling.

    Fails **open** on a Redis outage. The thing this defends is an API bill,
    and refusing a pilgrim's question because a cache is down would trade a real
    harm for a financial one. Contrast `sos` in Section 9, which is never
    hard-blocked at all — same principle, applied to a lesser case.
    """
    key = f"assistant:turns:{session_id}"
    try:
        count = await aw(redis.incr(key))
        if count == 1:
            await aw(redis.expire(key, 3600))
        return bool(count > ceiling)
    except Exception as exc:
        logger.warning("assistant_rate_limit_unavailable", extra={"error": str(exc)})
        return False


async def _record(
    session: AsyncSession,
    *,
    session_id: str,
    actor_id: uuid.UUID | None,
    language: str,
    question: str,
    result: AssistantAnswer,
) -> AssistantTurn:
    """Write the turn.  Section 13: every turn, with its tool calls, for review.

    The question is redacted of phone-shaped digit runs first. Both requirements
    are satisfiable at once — the shape of the question is what a reviewer
    needs, and the digits are what the DPDP Act is about.
    """
    # Counted here rather than at each return site, so a turn cannot be answered
    # without being counted — the same reason the transcript write lives here.
    metrics.observe_assistant_turn(str(result.outcome))

    turn = AssistantTurn(
        session_id=session_id[:64],
        actor_id=actor_id,
        language=language,
        question=guardrails.redact(question),
        answer=result.answer,
        outcome=str(result.outcome),
        refusal_reason=result.refusal_reason,
        tool_calls=result.tool_calls,
        model=result.model,
        latency_ms=result.latency_ms,
        created_at=now_utc(),
    )
    session.add(turn)
    await session.flush()
    return turn


async def ask(
    session: AsyncSession,
    *,
    question: str,
    session_id: str,
    actor_id: uuid.UUID | None = None,
    phone_hash: str | None = None,
    language: str = "mr",
    lon: float | None = None,
    lat: float | None = None,
) -> tuple[AssistantAnswer, AssistantTurn]:
    """Answer one question and record the turn."""
    started = now_utc()
    question = (question or "").strip()[:MAX_QUESTION_CHARS]

    if not await config_service.get(session, "assistant_enabled"):
        raise AppError("ASSISTANT_DISABLED")

    ceiling = await config_service.get_int(session, "assistant_max_turns_per_hour")
    if await _rate_limited(session_id, ceiling):
        raise AppError("ASSISTANT_RATE_LIMITED", details={"per_hour": ceiling})

    # --- 1. triage, before any model is involved -------------------------
    verdict = guardrails.triage(question)

    if verdict.verdict == TriageVerdict.REDIRECT_EMERGENCY:
        english, marathi = prompt.EMERGENCY_TEXT
        result = AssistantAnswer(
            answer=english,
            answer_mr=marathi,
            outcome=TurnOutcome.REFUSED,
            refusal_reason=verdict.reason,
            latency_ms=_ms(started),
        )
        logger.info(
            "assistant_emergency_redirect",
            extra={"session_id": session_id, "matched": verdict.matched},
        )
        return result, await _record(
            session,
            session_id=session_id,
            actor_id=actor_id,
            language=language,
            question=question,
            result=result,
        )

    if verdict.verdict == TriageVerdict.REFUSE:
        english, marathi = prompt.REFUSAL_TEXT.get(
            verdict.reason or "empty", prompt.REFUSAL_TEXT["empty"]
        )
        result = AssistantAnswer(
            answer=english,
            answer_mr=marathi,
            outcome=TurnOutcome.REFUSED,
            refusal_reason=verdict.reason,
            latency_ms=_ms(started),
        )
        return result, await _record(
            session,
            session_id=session_id,
            actor_id=actor_id,
            language=language,
            question=question,
            result=result,
        )

    # --- 2. the tools this turn is allowed to reach ----------------------
    ctx = ToolContext(
        session=session,
        phone_hash=phone_hash,
        actor_id=actor_id,
        language=language,
        lon=lon,
        lat=lat,
    )
    collected: list[dict[str, Any]] = []

    async def run_tool(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        outcome, ms = await tools.call(ctx, name, args)
        entry = outcome.as_log(ms)
        collected.append(entry)
        # `found` travels into the model's view of the result, not just the log.
        # It is the flag the system prompt keys "say you don't know" off, and
        # burying it in the payload shape would leave the model inferring
        # emptiness from an absent list.
        return {"found": outcome.found, **outcome.data}, entry

    # --- 3. the model, --- 4. or the manual mode -------------------------
    try:
        text, _log = await gemini.converse(
            prompt.SYSTEM_PROMPT, question, run_tool=run_tool
        )
        result = AssistantAnswer(
            answer=text,
            answer_mr=None,
            outcome=TurnOutcome.ANSWERED if any(c["found"] for c in collected) else TurnOutcome.NO_DATA,
            tool_calls=collected,
            model=settings.gemini_model,
            latency_ms=_ms(started),
        )
    except gemini.GeminiUnavailable as exc:
        logger.info(
            "assistant_degraded",
            extra={"reason": str(exc), "configured": gemini.is_configured()},
        )
        english, marathi, log = await fallback.answer(ctx, question)
        result = AssistantAnswer(
            answer=english,
            answer_mr=marathi,
            outcome=TurnOutcome.DEGRADED,
            tool_calls=collected + log,
            latency_ms=_ms(started),
            is_degraded=True,
        )
    except Exception:
        # The last resort. A crash here still produces a usable answer and still
        # writes a row, because the turn that errored is the one a reviewer most
        # wants to find.
        logger.exception("assistant_turn_failed", extra={"session_id": session_id})
        english, marathi = prompt.NO_DATA_TEXT
        result = AssistantAnswer(
            answer=english,
            answer_mr=marathi,
            outcome=TurnOutcome.ERROR,
            tool_calls=collected,
            latency_ms=_ms(started),
            is_degraded=True,
        )

    # --- 5. the transcript, always ---------------------------------------
    return result, await _record(
        session,
        session_id=session_id,
        actor_id=actor_id,
        language=language,
        question=question,
        result=result,
    )


def _ms(started: datetime) -> int:
    return int((now_utc() - started).total_seconds() * 1000)


async def purge_transcripts(session: AsyncSession, *, at: datetime | None = None) -> int:
    """Drop assistant turns past their retention (Section 12).

    Returns the count. The transcripts exist so a reviewer can answer "why did
    it say that" during and shortly after the Wari; keeping a pilgrim's
    questions for a year afterwards serves nobody and is exactly the
    accumulation the DPDP Act is about.
    """
    moment = at or now_utc()
    days = await config_service.get_int(session, "assistant_turn_retention_days")
    cutoff = moment - timedelta(days=days)

    doomed = list(
        (
            await session.execute(select(AssistantTurn.id).where(AssistantTurn.created_at < cutoff))
        ).scalars()
    )
    if doomed:
        await session.execute(delete(AssistantTurn).where(AssistantTurn.id.in_(doomed)))
    return len(doomed)
