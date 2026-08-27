"""The assistant's contract (Section 13, Phase 9).

Section 13 gives the assistant a MAY NOT DO list, and most of it is enforced
structurally rather than by asking a model nicely.  These tests are what makes
that claim checkable, and they are deliberately the kind of test that fails
loudly if somebody adds a convenient write tool eighteen months from now.

The four claims:

1. **No tool writes.**  The MAY NOT DO list is enforced by the absence of a
   tool, not by a sentence in a prompt. A refusal that depends on the model
   choosing to refuse is not a control.
2. **An emergency is redirected, never refused.**  Somebody typing "my father
   has collapsed" gets 108 and the SOS button in the first line. A refusal is a
   dead end to a person in the worst minute of their life.
3. **Absence is never narrated as data.**  A tool that found nothing produces
   "I do not know" plus the helpline, in the fallback exactly as in the prompt.
4. **The transcript keeps the question and drops the phone number.**  Section
   13 wants every turn logged; Section 12 wants no PII accumulated. Both.

None of this needs Postgres or a network — which is the point, since the
degraded path is the one most likely to be running on any given day.
"""

from __future__ import annotations

import pytest

from app.services.assistant import fallback, guardrails, prompt, tools
from app.services.assistant.guardrails import TriageVerdict
from app.services.assistant.tools import ToolResult


# ---------------------------------------------------------------------------
# claim 1 — the tool list is the control
# ---------------------------------------------------------------------------
def test_the_tool_list_is_exactly_the_five_section_13_names():
    """Section 13 names them: get_pass_status, get_zone_crowd,
    find_nearest_facility, get_schedule, raise_sos_draft."""
    assert {
        "get_pass_status",
        "get_zone_crowd",
        "find_nearest_facility",
        "get_schedule",
        "raise_sos_draft",
    } == tools.TOOL_NAMES


def test_no_tool_mutates_anything():
    """The MAY NOT DO list, enforced by absence.

    If this test fails because somebody added a tool, the question to ask is not
    "how do I update the assertion" — it is whether a language model should be
    able to do that thing to a temple's crowd-safety system.
    """
    forbidden = ("cancel", "book", "issue", "reslot", "dispatch", "set_", "update", "delete", "create")
    for name in tools.TOOL_NAMES:
        assert not any(name.startswith(word) or f"_{word}" in name for word in forbidden), name


def test_every_declared_tool_has_an_implementation_and_vice_versa():
    declared = {d["name"] for d in tools.DECLARATIONS}
    assert declared == set(tools.TOOLS) == tools.TOOL_NAMES


class RecordingSession:
    """A session that fails loudly if anything tries to write through it."""

    def __init__(self) -> None:
        self.writes: list[object] = []

    def add(self, obj: object) -> None:
        self.writes.append(obj)


async def test_raise_sos_draft_writes_nothing():
    """The name is the contract. A language model must not be able to put a row
    in a control room's alarm queue — so the tool that helps compose an SOS is
    checked for not having touched the session at all.
    """
    session = RecordingSession()
    ctx = tools.ToolContext(session=session)  # type: ignore[arg-type]

    result, _ms = await tools.call(
        ctx, tools.RAISE_SOS_DRAFT, {"type": "medical", "description": "father collapsed"}
    )

    assert result.found
    assert session.writes == []
    assert result.data["sent"] is False


def test_the_sos_draft_declaration_tells_the_model_it_sends_nothing():
    declaration = next(d for d in tools.DECLARATIONS if d["name"] == tools.RAISE_SOS_DRAFT)
    assert "do NOT send" in declaration["description"]
    assert "108" in declaration["description"]


async def test_the_sos_draft_tool_reports_that_nothing_was_sent():
    result, _ms = await tools.call(None, tools.RAISE_SOS_DRAFT, {"type": "medical", "description": "x"})

    assert result.found
    assert result.data["sent"] is False
    assert result.data["call_now"] == "108"
    assert "NOTHING HAS BEEN SENT" in result.data["instruction"]


async def test_an_invented_tool_name_is_a_miss_not_a_crash():
    """Models hallucinate function names. The right answer is "that tool does
    not exist", not a 500 on a pilgrim's question."""
    result, _ms = await tools.call(None, "cancel_pass", {})

    assert not result.found
    assert result.data["reason"] == "no_such_tool"
    assert "get_pass_status" in result.data["available"]


# ---------------------------------------------------------------------------
# claim 2 — emergencies are redirected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "my father has collapsed near the east gate",
        "he is not breathing please help",
        "माझा मुलगा हरवला आहे",  # my son is lost
        "बाबा बेशुद्ध पडले आहेत",  # father has fallen unconscious
        "मदद करो मेरी माँ गिर गयी",  # help, my mother has fallen
        "there is bleeding, we need an ambulance",
        "chest pain, what do I do",
    ],
)
def test_an_emergency_is_redirected_before_any_model_is_involved(question):
    verdict = guardrails.triage(question)

    assert verdict.verdict == TriageVerdict.REDIRECT_EMERGENCY
    assert verdict.reason == "medical_emergency"
    assert not verdict.is_allowed


def test_the_emergency_reply_leads_with_the_number_and_the_button():
    """A person in the worst minute of their life reads the first line."""
    english, marathi = prompt.EMERGENCY_TEXT

    assert english.startswith("Call 108")
    assert "SOS" in english and "SOS" in marathi
    assert "१०८" in marathi


def test_an_emergency_mentioning_a_pass_is_still_an_emergency():
    """"My father collapsed, can I cancel his pass" is an emergency that happens
    to mention a pass. Answering the pass half would be the worst possible
    reading of the sentence — which is why emergency is checked first."""
    verdict = guardrails.triage("my father collapsed, should I cancel his pass")
    assert verdict.verdict == TriageVerdict.REDIRECT_EMERGENCY


# ---------------------------------------------------------------------------
# refusals, and what they are not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("cancel my pass please", "pass_mutation"),
        ("can you book a slot for tomorrow", "pass_mutation"),
        ("माझा पास रद्द करा", "pass_mutation"),
        ("close the gate at the east corridor", "safety_decision"),
        ("raise the threshold for zone 3", "safety_decision"),
        ("dispatch a team to the ghat", "dispatch"),
        ("send the police to gate 2", "dispatch"),
    ],
)
def test_the_may_not_do_list_is_refused(question, reason):
    verdict = guardrails.triage(question)
    assert verdict.verdict == TriageVerdict.REFUSE
    assert verdict.reason == reason


@pytest.mark.parametrize(
    "question",
    [
        "what is my pass status",
        "when is my slot",
        "how long is the wait for darshan",
        "माझ्या पासची वेळ काय आहे",
        "how crowded is the east corridor",
        "where is the nearest water point",
        "what time does darshan open",
    ],
)
def test_ordinary_questions_reach_the_model(question):
    """A noun on its own is not a mutation. "What is my pass status" must keep
    working, or the refusal list has eaten the product."""
    assert guardrails.triage(question).is_allowed


def test_every_refusal_reason_has_a_bilingual_reply():
    for reason in guardrails.REFUSALS:
        if reason == "medical_emergency":
            continue  # redirected, not refused
        english, marathi = prompt.REFUSAL_TEXT[reason]
        assert english and marathi


def test_a_refusal_always_says_who_can_help_instead():
    """A refusal that ends without a next step is a dead end, and this is a
    person with a real problem."""
    for reason in ("pass_mutation", "safety_decision", "dispatch"):
        english, _mr = prompt.REFUSAL_TEXT[reason]
        assert any(word in english for word in ("app", "volunteer", "control room", "108", "help desk"))


def test_an_empty_question_is_answered_with_what_the_assistant_can_do():
    verdict = guardrails.triage("   ")
    assert verdict.verdict == TriageVerdict.REFUSE
    assert verdict.reason == "empty"


# ---------------------------------------------------------------------------
# claim 3 — absence is never narrated as data
# ---------------------------------------------------------------------------
def test_the_system_prompt_carries_section_13s_two_sentences():
    assert "Never fabricate a wait time" in prompt.SYSTEM_PROMPT
    assert "108" in prompt.SYSTEM_PROMPT
    assert "SAY YOU DO NOT KNOW" in prompt.SYSTEM_PROMPT
    assert "UNKNOWN" in prompt.SYSTEM_PROMPT


def test_the_prompt_forbids_writing_crowd_safety_advice():
    """The advice sentences come from `crowd_service.PUBLIC_ADVICE`. A model
    composing its own wording for a CRITICAL zone is a language model writing a
    safety instruction."""
    assert "do not write your own crowd-safety advice" in prompt.SYSTEM_PROMPT


@pytest.mark.parametrize("name", sorted(tools.TOOL_NAMES))
def test_a_tool_that_found_nothing_renders_the_helpline(name):
    """Claim 3, in the degraded path. The rule belongs to the product, not to
    the model — so the fallback obeys it too."""
    english, marathi = fallback.render(
        ToolResult(name, found=False, data={}, summary="nothing")
    )
    assert english == prompt.NO_DATA_TEXT[0]
    assert "108" in english
    assert "१०८" in marathi


def test_a_stale_crowd_reading_is_not_rendered_as_an_answer():
    """Section 4/M7: never render stale crowd data as if it were live."""
    stale_only = ToolResult(
        tools.GET_ZONE_CROWD,
        found=True,
        data={"zones": [{"is_stale": True, "name": "East", "level": None}]},
        summary="0 live of 1",
    )
    english, _mr = fallback.render(stale_only)
    assert english == prompt.NO_DATA_TEXT[0]


def test_the_crowd_answer_quotes_the_official_advice_sentence():
    """Two renderers writing their own crowd-safety wording is exactly the
    drift Section 13 forbids of the model."""
    from app.models.crowd import DensityLevel
    from app.services import crowd_service

    advice, advice_mr = crowd_service.PUBLIC_ADVICE[DensityLevel.CRITICAL]
    result = ToolResult(
        tools.GET_ZONE_CROWD,
        found=True,
        data={
            "zones": [
                {
                    "is_stale": False,
                    "name": "East Corridor",
                    "name_mr": "पूर्व मार्गिका",
                    "level": "critical",
                    "advice": advice,
                    "advice_mr": advice_mr,
                    "observed_at": "2026-07-25T14:05:00+00:00",
                }
            ]
        },
        summary="1 live of 1",
    )
    english, marathi = fallback.render(result)

    assert advice in english
    assert advice_mr in marathi


def test_a_pass_answer_says_the_wait_is_an_estimate():
    """Section 0 rule 3. The one number Section 13 singles out by name."""
    result = ToolResult(
        tools.GET_PASS_STATUS,
        found=True,
        data={
            "reference": "WV3F8K2Q1M",
            "status": "active",
            "is_active": True,
            "slot_start": "2026-07-25T09:30:00+00:00",
            "estimated_entry_at": "2026-07-25T10:05:00+00:00",
            "people_ahead": 1840,
            "gate": "G2",
        },
        summary="ok",
    )
    english, marathi = fallback.render(result)

    assert "09:30" in english
    assert "10:05" in english
    assert "1840" in english
    assert "estimate, not a promise" in english
    assert "अंदाज आहे, खात्री नाही" in marathi


# ---------------------------------------------------------------------------
# the degraded path routes real questions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("how long is the wait", tools.GET_PASS_STATUS),
        ("माझा पास कधी आहे", tools.GET_PASS_STATUS),
        ("how crowded is it", tools.GET_ZONE_CROWD),
        ("किती गर्दी आहे", tools.GET_ZONE_CROWD),
        ("where is the nearest toilet", tools.FIND_NEAREST_FACILITY),
        ("पाणी कुठे मिळेल", tools.FIND_NEAREST_FACILITY),
        ("what time does darshan open", tools.GET_SCHEDULE),
        ("आरतीची वेळ", tools.GET_SCHEDULE),
    ],
)
def test_the_fallback_routes_the_common_questions(question, expected):
    routed = fallback.route(question)
    assert routed is not None
    assert routed[0] == expected


@pytest.mark.parametrize(
    ("question", "ftype"),
    [
        ("where can I get water", "water"),
        ("शौचालय कुठे आहे", "toilet"),
        ("I need a doctor", "medical"),
        ("where is food", "food"),
        ("charging point", "charging"),
    ],
)
def test_the_fallback_picks_the_facility_type_rather_than_guessing(question, ftype):
    routed = fallback.route(question)
    assert routed is not None
    assert routed[1]["type"] == ftype


def test_the_fallback_says_so_when_it_cannot_route():
    """It covers the common questions, not all of them. Pretending otherwise is
    how it would start guessing."""
    assert fallback.route("is the east path easier for my mother") is None

    english, marathi = fallback.UNROUTED
    assert "108" in english
    assert "१०८" in marathi


# ---------------------------------------------------------------------------
# claim 4 — the transcript
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "my son is missing call me on 9876543210",
        "call me on +91 98765 43210",
        "reach me at 98765-43210",
    ],
)
def test_a_phone_number_is_stripped_before_the_question_is_stored(question):
    """Section 13 wants every turn logged; Section 12 wants no PII accumulated.
    The sentence survives, the number does not."""
    cleaned = guardrails.redact(question)

    assert "9876543210" not in cleaned.replace(" ", "").replace("-", "")
    assert "[number]" in cleaned


def test_redaction_keeps_the_sentence_a_reviewer_needs():
    cleaned = guardrails.redact("my son is missing call me on 9876543210")
    assert "son is missing" in cleaned


def test_redaction_catches_long_digit_runs_that_are_not_phone_shaped():
    """A pilgrim will type an Aadhaar number into a chat box sooner or later."""
    assert "123456789012" not in guardrails.redact("my aadhaar is 123456789012")


def test_short_numbers_survive_redaction():
    """Slot times, gate numbers and 108 itself are not PII, and stripping them
    would make the transcript unreadable."""
    cleaned = guardrails.redact("I have 4 people and my slot is at 9:30, gate 2")
    assert "4 people" in cleaned
    assert "9:30" in cleaned


def test_a_very_long_question_is_truncated_rather_than_refused():
    cleaned = guardrails.redact("a" * 5000)
    assert len(cleaned) == guardrails.MAX_STORED_QUESTION
