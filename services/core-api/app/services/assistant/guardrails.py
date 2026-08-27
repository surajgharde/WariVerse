"""What happens before the model, and what the model cannot do (Section 13).

Section 13 gives the assistant a MAY NOT DO list.  Three of the four items on
it are enforced structurally rather than by instruction, and that distinction is
the whole design:

* *"Decide or alter a crowd-safety action, a density threshold, or a dispatch"*
  and *"issue, cancel, or reslot a pass"* — enforced by the tool list.  Every
  tool in `tools.py` is a read.  There is no write for the model to reach, so
  no prompt injection, no jailbreak and no bug in the system prompt can produce
  one.  A refusal that depends on the model choosing to refuse is not a control.

* *"Answer from its own knowledge about live state"* — enforced by the answer
  path.  A claim about a wait time, a crowd level or a pass reaches the user
  only if a tool returned it this turn.

* *"Assess a medical emergency"* — this one **cannot** be enforced by the tool
  list, because the input is a sentence, not a function call.  So it is caught
  here, before a model is involved at all, by the triage pass below.

On that last point, note what the correct behaviour actually is.  Somebody who
types "my father has collapsed" must not receive a refusal — a refusal is a
dead end to a person in the worst minute of their life.  They receive the
emergency path: 108, the SOS button, and the control-room number.  That is a
*redirect*, and it is the reason `TriageVerdict` has three values rather than
two.

The keyword lists are Marathi-first, then Hindi, then English, in that order,
because that is the order the queue in Pandharpur actually speaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class TriageVerdict(StrEnum):
    ALLOW = "allow"
    #: Not a refusal.  A medical or safety emergency, routed to the path that
    #: actually helps rather than into a conversation.
    REDIRECT_EMERGENCY = "redirect_emergency"
    #: Outside the contract entirely.  Answered with what the assistant *can*
    #: do, never with a lecture.
    REFUSE = "refuse"


#: Refusal reasons, stored on the turn so a reviewer reads why rather than
#: "blocked".  A rising rate in any one of these is a product telling users the
#: wrong thing about what the assistant is for.
REFUSALS = {
    "medical_emergency": "asked the assistant to assess a medical emergency",
    "pass_mutation": "asked the assistant to issue, cancel or reslot a pass",
    "safety_decision": "asked the assistant to make or change a crowd-safety decision",
    "dispatch": "asked the assistant to dispatch or task a responder",
}


# --- emergency ------------------------------------------------------------
#: Words that mean somebody is hurt, lost or in danger *now*.  Deliberately
#: broad: the cost of redirecting a curious question to the emergency screen is
#: one wasted tap, and the cost of missing a real one is not comparable.
_EMERGENCY = (
    # Marathi
    "बेशुद्ध", "श्वास", "छातीत दुखत", "रक्त", "अपघात", "पडले", "पडला", "पडली",
    "हरव", "हरवल", "सापडत नाही", "मदत करा", "वाचवा", "गुदमर", "चेंगर",
    "हृदय", "झटका", "जखमी", "आग लागली",
    # Hindi
    "बेहोश", "साँस", "सांस", "सीने में दर्द", "खून", "दुर्घटना", "गिर गय",
    "खो गय", "मदद करो", "बचाओ", "दम घुट", "दिल का दौरा", "घायल", "आग लग",
    # English
    "unconscious", "not breathing", "can't breathe", "cannot breathe",
    "chest pain", "heart attack", "bleeding", "collapsed", "seizure",
    "stroke", "crushed", "stampede", "trampled", "drowning", "fire",
    "my child is missing", "child is missing", "lost my child", "lost my son",
    "lost my daughter", "missing person", "help me", "save him", "save her",
    "emergency", "ambulance", "dying", "injured",
)

# --- pass mutation --------------------------------------------------------
_PASS_VERBS = (
    "cancel", "book", "issue", "reslot", "reschedule", "change my slot",
    "move my pass", "refund", "रद्द", "बुक", "बदल", "वेळ बदला",
    "रद्द करा", "कैंसल", "बदलो",
)
_PASS_NOUNS = ("pass", "slot", "darshan", "booking", "पास", "स्लॉट", "दर्शन", "वेळ", "नोंदणी")

# --- safety decisions and dispatch ---------------------------------------
_SAFETY = (
    "close the gate", "open the gate", "hold the gate", "stop intake",
    "change the threshold", "raise the threshold", "lower the threshold",
    "override the alert", "clear the alert", "acknowledge the alert",
    "द्वार बंद", "द्वार उघड", "प्रवेश थांबवा", "मर्यादा बदल",
)
_DISPATCH = (
    "send an ambulance to", "send the police", "dispatch", "send a team",
    "send responders", "रुग्णवाहिका पाठवा", "पथक पाठवा", "पोलीस पाठवा",
)


@dataclass(frozen=True, slots=True)
class Triage:
    verdict: TriageVerdict
    reason: str | None = None
    #: The matched phrase, kept for review so a reviewer can see *why* a turn
    #: was routed the way it was and tune the lists against real traffic.
    matched: str | None = None
    hints: list[str] = field(default_factory=list)

    @property
    def is_allowed(self) -> bool:
        return self.verdict == TriageVerdict.ALLOW


def _first_match(text: str, needles: tuple[str, ...]) -> str | None:
    for needle in needles:
        if needle in text:
            return needle
    return None


def triage(question: str) -> Triage:
    """Decide whether this question reaches the model at all.

    Order matters and is not arbitrary.  Emergency is checked first, because
    "my father collapsed, can I cancel his pass" is an emergency that happens to
    mention a pass, and answering the pass half of it would be the worst
    possible reading of the sentence.
    """
    text = question.lower().strip()
    if not text:
        return Triage(TriageVerdict.REFUSE, reason="empty")

    emergency = _first_match(text, _EMERGENCY)
    if emergency is not None:
        return Triage(
            TriageVerdict.REDIRECT_EMERGENCY,
            reason="medical_emergency",
            matched=emergency,
        )

    dispatch = _first_match(text, _DISPATCH)
    if dispatch is not None:
        return Triage(TriageVerdict.REFUSE, reason="dispatch", matched=dispatch)

    safety = _first_match(text, _SAFETY)
    if safety is not None:
        return Triage(TriageVerdict.REFUSE, reason="safety_decision", matched=safety)

    verb = _first_match(text, _PASS_VERBS)
    if verb is not None and _first_match(text, _PASS_NOUNS) is not None:
        # A verb and a noun together. "What is my pass status" contains a noun
        # and no verb, and is a perfectly good question the assistant answers
        # from `get_pass_status` — requiring both is what keeps that working.
        return Triage(TriageVerdict.REFUSE, reason="pass_mutation", matched=verb)

    return Triage(TriageVerdict.ALLOW)


# ---------------------------------------------------------------------------
# storing the question
# ---------------------------------------------------------------------------
#: Ten or more digits in a row, allowing the spaces and dashes people type.
#: Indian mobile numbers are ten digits; +91 makes twelve.
_PHONE = re.compile(r"(?:\+?\d[\s-]?){10,}")
#: Long digit runs that are not phone-shaped — Aadhaar, account numbers. A
#: pilgrim will type one of these into a chat box sooner or later.
_LONG_DIGITS = re.compile(r"\d{9,}")

#: Section 13 requires every turn to be logged for review. Section 12 requires
#: not accumulating personal data. Both are satisfiable: the *shape* of the
#: question is what a reviewer needs, and the digits are what the DPDP Act is
#: about.
MAX_STORED_QUESTION = 2000


def redact(question: str) -> str:
    """Strip contact-number-shaped digit runs before the question is stored.

    "My son is missing, call me on 98xxxxxxxx" is the realistic case, and it is
    also the case where the transcript is most likely to be read by several
    people afterwards. The sentence survives; the number does not.
    """
    cleaned = _PHONE.sub("[number]", question)
    cleaned = _LONG_DIGITS.sub("[number]", cleaned)
    return cleaned[:MAX_STORED_QUESTION]
