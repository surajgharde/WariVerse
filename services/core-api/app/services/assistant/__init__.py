"""The pilgrim assistant (Section 13, Phase 9).

A deliberately narrow contract, in four files:

* `guardrails.py` — what happens *before* a model is involved, and what the
  model is structurally unable to do.
* `tools.py` — the fixed tool list. Five functions, each a thin read over a
  service that already exists. This is the only way a factual claim about live
  state can enter an answer.
* `gemini.py` — the function-calling loop, over httpx.
* `service.py` — orchestration, the deterministic fallback, and the transcript.

The single sentence this package is built around is from Section 13:

    "Never fabricate a wait time — a wrong number here sends a person into a
    crowd at the wrong moment."

Everything else follows from taking that literally. The model is given no tool
that writes, no tool that decides, and no knowledge of live state except what a
tool returned in this turn. When a tool comes back empty the answer is "I don't
know" plus the helpline, because the alternative is a plausible sentence, and a
plausible sentence about a queue is indistinguishable from a true one to
somebody standing in it.
"""

from app.services.assistant.guardrails import (
    REFUSALS,
    Triage,
    TriageVerdict,
    redact,
    triage,
)
from app.services.assistant.service import AssistantAnswer, ask

__all__ = [
    "REFUSALS",
    "AssistantAnswer",
    "Triage",
    "TriageVerdict",
    "ask",
    "redact",
    "triage",
]
