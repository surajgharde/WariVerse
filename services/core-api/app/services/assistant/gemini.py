"""Gemini function calling, over httpx (Section 13).

Deliberately a thin client rather than the vendor SDK.  The whole interaction is
one POST with a tool list and a loop over the response, `httpx` is already a
dependency for the AI-engine calls, and adding `google-generativeai` would pull
a transitive tree into a service whose other 40 modules have none of it.  If the
provider changes, what changes is this file.

The loop is bounded at `MAX_ROUNDS`.  A model that keeps asking for tools is a
model that is not converging, and a pilgrim standing in a queue is owed a
timeout rather than a spinner — after the last round the caller falls back to
the deterministic answer, which is always available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

#: Two rounds of tool calls, then answer.  Every real question in Section 13's
#: MAY DO list is answerable from one or two lookups — "where is the nearest
#: water and how crowded is it there" is the deep end.
MAX_ROUNDS = 3

#: A pilgrim on 2G in a saturated cell during the Wari. Longer than a normal API
#: timeout because the alternative is falling back for a request that would have
#: succeeded, and shorter than a person's patience.
TIMEOUT_SECONDS = 12.0

#: Enough for a paragraph in Devanagari, which costs more tokens per character
#: than Latin script. Section 10 asks for short answers anyway.
MAX_OUTPUT_TOKENS = 800


class GeminiUnavailable(RuntimeError):
    """The model could not be reached, is not configured, or refused.

    Always recoverable by the caller: `service.ask` catches this and answers
    from the deterministic path instead. Section 11 — every module has a
    defined manual mode, and this is the assistant's.
    """


@dataclass(slots=True)
class ModelTurn:
    """One round-trip: what the model said, and what it asked to call."""

    text: str | None = None
    function_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    finish_reason: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.function_calls)


def is_configured() -> bool:
    return bool(settings.gemini_api_key)


def _tool_config() -> list[dict[str, Any]]:
    from app.services.assistant.tools import DECLARATIONS

    return [{"function_declarations": DECLARATIONS}]


def _parse(payload: dict[str, Any]) -> ModelTurn:
    """Pull text and function calls out of one `generateContent` response.

    Written defensively on purpose. A response shape that has drifted must
    degrade to "the model gave us nothing usable" — which the caller handles —
    rather than raise a KeyError out of a pilgrim's question.
    """
    candidates = payload.get("candidates") or []
    if not candidates:
        # No candidate at all usually means the safety filter took it. Treated
        # as unavailable rather than as an answer: falling back to the
        # deterministic reply is strictly better than saying nothing.
        raise GeminiUnavailable("no candidates in response")

    candidate = candidates[0]
    turn = ModelTurn(finish_reason=candidate.get("finishReason"))
    parts = (candidate.get("content") or {}).get("parts") or []

    texts: list[str] = []
    for part in parts:
        if part.get("text"):
            texts.append(part["text"])
        call = part.get("functionCall")
        if call and call.get("name"):
            turn.function_calls.append((call["name"], call.get("args") or {}))

    turn.text = "\n".join(texts).strip() or None
    return turn


async def converse(
    system_prompt: str,
    question: str,
    *,
    run_tool: Any,
    max_rounds: int = MAX_ROUNDS,
) -> tuple[str, list[dict[str, Any]]]:
    """Run the function-calling loop and return the answer plus the tool log.

    `run_tool` is an async callable `(name, args) -> (result_dict, log_entry)`.
    Passing it in rather than importing the dispatcher keeps this file ignorant
    of what the tools actually do, which is what lets the guardrail tests drive
    it with fakes.

    Raises `GeminiUnavailable` for anything the caller should fall back from:
    no key, a network failure, an HTTP error, an empty response, or a model that
    burned every round on tool calls without producing an answer.
    """
    if not is_configured():
        raise GeminiUnavailable("GEMINI_API_KEY is not set")

    contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": question}]}]
    tool_log: list[dict[str, Any]] = []

    body_base: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "tools": _tool_config(),
        "generationConfig": {
            # Section 13 is about not fabricating. Sampling temperature is not
            # the control that prevents that — the tool contract is — but there
            # is no upside to creative phrasing when the job is relaying a slot
            # time in Marathi.
            "temperature": 0.2,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }

    url = f"{_BASE}/{settings.gemini_model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            for round_index in range(max_rounds):
                response = await client.post(url, headers=headers, json={**body_base, "contents": contents})
                if response.status_code >= 400:
                    # The body can carry the provider's own error text, which is
                    # worth logging and must never reach the pilgrim.
                    logger.warning(
                        "gemini_http_error",
                        extra={"status": response.status_code, "round": round_index},
                    )
                    raise GeminiUnavailable(f"HTTP {response.status_code}")

                turn = _parse(response.json())

                if not turn.wants_tools:
                    if not turn.text:
                        raise GeminiUnavailable("model returned no text")
                    return turn.text, tool_log

                contents.append(
                    {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": name, "args": args}}
                            for name, args in turn.function_calls
                        ],
                    }
                )

                response_parts: list[dict[str, Any]] = []
                for name, args in turn.function_calls:
                    result, entry = await run_tool(name, args)
                    tool_log.append(entry)
                    response_parts.append(
                        {"functionResponse": {"name": name, "response": result}}
                    )
                contents.append({"role": "user", "parts": response_parts})

    except httpx.HTTPError as exc:
        raise GeminiUnavailable(f"transport: {type(exc).__name__}") from exc

    raise GeminiUnavailable(f"no answer after {max_rounds} rounds")
