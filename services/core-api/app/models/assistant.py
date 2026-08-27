"""The assistant's transcript (Section 13, Phase 9).

Section 13 ends with one sentence that decides this table exists: "Log every
assistant turn with its tool calls for review."

The reason is narrower than general observability.  The assistant is the only
component in this system that produces sentences nobody wrote, and its contract
is that every factual claim it makes about live state came from a tool call to
our own API.  That contract is unfalsifiable unless the tool calls are stored
next to the answer.  With them stored, "the app told my mother the queue was
short" is a question with an answer: here is the turn, here is the
`get_zone_crowd` result it was given, here is what it said.

Two deliberate choices about content:

* The question is stored with phone-number-shaped digit runs redacted.  A
  pilgrim typing "my son is missing, call me on 98xxxxxxxx" is the realistic
  case, and a review table full of raw contact numbers is exactly the PII spill
  Section 12 exists to prevent.
* Tool *results* are stored as a summary, not verbatim.  Copying a
  `get_pass_status` payload in here would put a pass reference and slot time in
  a table with a longer retention than the pass itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TurnOutcome(StrEnum):
    """What actually happened, from the reviewer's point of view.

    `NO_DATA` and `REFUSED` are separate because they mean opposite things about
    the system.  `NO_DATA` is the assistant behaving correctly when a tool came
    back empty — it said it did not know and gave the helpline.  `REFUSED` is
    the guardrail catching a question that is outside the contract at all.
    A rising `NO_DATA` rate is an outage; a rising `REFUSED` rate is a product
    telling users the wrong thing about what it can do.
    """

    ANSWERED = "answered"
    NO_DATA = "no_data"
    REFUSED = "refused"
    #: The model was unreachable or unconfigured and the deterministic fallback
    #: answered instead.  Section 4/M8 + Section 11: every module has a manual
    #: mode, and this is the assistant's.
    DEGRADED = "degraded"
    ERROR = "error"


class AssistantTurn(Base):
    __tablename__ = "assistant_turns"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Groups the turns of one conversation.  Client-generated and opaque; it is
    #: not a user id, and an anonymous pilgrim gets one without signing in.
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="mr")

    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    #: Which guardrail fired, when one did.  Named after the rule so a reviewer
    #: reads "pass_mutation" rather than "blocked".
    refusal_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    #: `[{"name": ..., "args": {...}, "found": true, "ms": 12}]` — the evidence
    #: that a factual claim had a source. Results are summarised, never copied.
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)

    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (Index("ix_assistant_turns_outcome_created", "outcome", "created_at"),)
