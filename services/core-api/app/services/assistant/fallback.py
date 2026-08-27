"""The assistant with no model behind it (Section 11, E8).

Every module in this system has a defined manual mode, and this is the
assistant's.  It runs when there is no `GEMINI_API_KEY`, when the provider is
unreachable, when the billing account lapses mid-Wari, and in every CI run — so
it is not a stub that exists to satisfy a paragraph in the design document.  It
is the path most likely to be running on any given day of the year.

The design is a keyword router over the *same five tools*, rendering templated
bilingual sentences.  It answers materially fewer questions than the model does,
and the ones it answers, it answers with identical facts, because the facts come
from the same place.  What it loses is phrasing: it cannot handle "my mother
gets tired, is the east way easier" — and it says so, rather than guessing.

Two things it deliberately does not do:

* It does not attempt to *look* like the model. The answers are plainly
  templated. A degraded mode that is indistinguishable from the working one is a
  degraded mode nobody notices has been running for three days.
* It does not lower its standards for absence. A tool that comes back empty
  still produces "I do not know, here is the helpline" — the exact rule the
  system prompt gives the model. That rule belongs to the product, not to the
  model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.assistant import tools
from app.services.assistant.prompt import NO_DATA_TEXT
from app.services.assistant.tools import ToolContext, ToolResult

#: Keyword -> tool.  Marathi and Hindi first: that is what the queue speaks, and
#: an English-first list quietly makes the fallback an English-only product.
_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        tools.GET_PASS_STATUS,
        (
            "पास", "स्लॉट", "माझा पास", "किती वेळ", "रांग", "प्रतीक्षा", "नंबर कधी",
            "पर्ची", "कितनी देर", "मेरा पास", "इंतजार",
            "pass", "slot", "my booking", "wait", "queue", "how long", "my turn", "darshan time",
        ),
    ),
    (
        tools.GET_ZONE_CROWD,
        (
            "गर्दी", "किती गर्दी", "भरले", "मोकळे", "सुरक्षित", "जाऊ का",
            "भीड", "कितनी भीड",
            "crowd", "crowded", "busy", "how full", "is it safe", "should i go", "packed",
        ),
    ),
    (
        tools.FIND_NEAREST_FACILITY,
        (
            "पाणी", "शौचालय", "स्वच्छतागृह", "दवाखाना", "वैद्यकीय", "जेवण", "अन्न",
            "विश्रांती", "हरवले", "मदत कक्ष", "चार्ज",
            "पानी", "शौचालय", "खाना", "दवा",
            "water", "toilet", "washroom", "medical", "doctor", "food", "rest", "charging",
            "lost and found", "help desk", "nearest",
        ),
    ),
    (
        tools.GET_SCHEDULE,
        (
            "वेळ", "आरती", "किती वाजता", "उघडते", "बंद होते", "वेळापत्रक",
            "समय", "कितने बजे", "खुलता",
            "timing", "time", "aarti", "opens", "closes", "schedule", "what time",
        ),
    ),
)

#: Which facility a word is asking for.  Checked before the generic route so
#: "where is water" reaches `find_nearest_facility` with `type=water` rather
#: than with a guess.
_FACILITY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("water", ("पाणी", "पानी", "water", "drinking", "thirsty", "तहान")),
    ("toilet", ("शौचालय", "स्वच्छतागृह", "toilet", "washroom", "restroom", "bathroom")),
    ("medical", ("दवाखाना", "वैद्यकीय", "औषध", "दवा", "medical", "doctor", "medicine", "first aid")),
    ("food", ("जेवण", "अन्न", "खाना", "भोजन", "food", "eat", "meal", "prasad")),
    ("rest_zone", ("विश्रांती", "आराम", "rest", "sit", "shade", "sleep")),
    ("lost_and_found", ("हरवले", "हरवली", "खो गया", "lost and found", "lost")),
    ("help_desk", ("मदत कक्ष", "help desk", "information", "माहिती कक्ष")),
    ("charging", ("चार्ज", "charging", "charge my phone", "battery")),
)


def _facility_type(text: str) -> str | None:
    for ftype, words in _FACILITY_WORDS:
        if any(word in text for word in words):
            return ftype
    return None


def route(question: str) -> tuple[str, dict[str, Any]] | None:
    """Which tool this question is asking for, and with what arguments.

    None when nothing matches.  That is a real and frequent answer — the
    fallback covers the common questions, not all of them, and pretending
    otherwise is how it would start guessing.
    """
    text = question.lower().strip()
    if not text:
        return None

    ftype = _facility_type(text)
    if ftype is not None:
        return (tools.FIND_NEAREST_FACILITY, {"type": ftype, "limit": 3})

    for name, keywords in _ROUTES:
        if any(word in text for word in keywords):
            return (name, {})
    return None


def _hhmm(iso: str | None) -> str:
    """An ISO timestamp as HH:MM.  The date is never what somebody in a queue
    is asking about, and a full timestamp in a two-line answer is noise."""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "—"


def render(result: ToolResult) -> tuple[str, str]:
    """Turn one tool result into a bilingual answer.

    Every branch that can be reached with `found=False` renders the same "I do
    not know, here is the helpline" as the model is instructed to give. The rule
    is the product's, not the model's.
    """
    if not result.found:
        return NO_DATA_TEXT

    data = result.data

    if result.name == tools.GET_PASS_STATUS:
        start = _hhmm(data.get("slot_start"))
        entry = _hhmm(data.get("estimated_entry_at"))
        ahead = data.get("people_ahead", 0)
        gate = data.get("gate") or "—"
        if not data.get("is_active"):
            return (
                f"Pass {data['reference']} is {data['status']}. Its slot was {start}. "
                "Ask at the help desk if you think that is wrong.",
                f"पास {data['reference']} ची स्थिती: {data['status']}. त्याची वेळ {start} होती. "
                "काही चूक वाटत असल्यास मदत कक्षात विचारा.",
            )
        return (
            f"Pass {data['reference']}: slot {start} at gate {gate}, {ahead} people ahead. "
            f"At the current rate you would enter around {entry}. This is an estimate, not a promise.",
            f"पास {data['reference']}: वेळ {start}, द्वार {gate}, तुमच्या पुढे {ahead} जण. "
            f"सध्याच्या वेगाने सुमारे {entry} वाजता प्रवेश मिळेल. हा अंदाज आहे, खात्री नाही.",
        )

    if result.name == tools.GET_ZONE_CROWD:
        zones = data.get("zones") or []
        live = [z for z in zones if not z["is_stale"]]
        if not live:
            return NO_DATA_TEXT
        first = live[0]
        # The advice sentence is quoted from `crowd_service.PUBLIC_ADVICE`, not
        # composed here. Two renderers writing their own crowd-safety wording is
        # exactly the drift Section 13 forbids of the model.
        return (
            f"{first['name']}: {first['level']}. {first['advice']} "
            f"(reading from {_hhmm(first.get('observed_at'))})",
            f"{first['name_mr']}: {first['level']}. {first['advice_mr']} "
            f"({_hhmm(first.get('observed_at'))} ची नोंद)",
        )

    if result.name == tools.FIND_NEAREST_FACILITY:
        items = data.get("facilities") or []
        if not items:
            return NO_DATA_TEXT
        first = items[0]
        distance = (
            f" — about {first['metres_away']} m away" if "metres_away" in first else ""
        )
        distance_mr = (
            f" — सुमारे {first['metres_away']} मीटर अंतरावर" if "metres_away" in first else ""
        )
        broken = "" if first["status"] == "operational" else f" (marked {first['status']})"
        broken_mr = "" if first["status"] == "operational" else f" ({first['status']} अशी नोंद)"
        return (
            f"Nearest {data['type'].replace('_', ' ')}: {first['name']}{distance}{broken}. "
            f"{len(items)} found nearby.",
            f"जवळचे ठिकाण: {first['name_mr']}{distance_mr}{broken_mr}. "
            f"जवळपास {len(items)} ठिकाणे आहेत.",
        )

    if result.name == tools.GET_SCHEDULE:
        aartis = ", ".join(f"{a['name']} {a['time']}" for a in data.get("aarti", []))
        aartis_mr = ", ".join(f"{a['name_mr']} {a['time']}" for a in data.get("aarti", []))
        return (
            f"Darshan is open {data['darshan_opens']} to {data['darshan_closes']}. "
            f"Aarti: {aartis}. These are scheduled times, not an estimate of your wait.",
            f"दर्शन {data['darshan_opens']} ते {data['darshan_closes']} पर्यंत सुरू असते. "
            f"आरती: {aartis_mr}. या ठरलेल्या वेळा आहेत, तुमच्या प्रतीक्षेचा अंदाज नाही.",
        )

    if result.name == tools.RAISE_SOS_DRAFT:
        return (
            "NOTHING HAS BEEN SENT YET. Call 108 now if anyone is hurt, and press the red SOS "
            "button in the app to send this report to the control room.",
            "अजून काहीही पाठवलेले नाही. कोणी जखमी असेल तर आत्ताच १०८ वर फोन करा, आणि हा अहवाल "
            "नियंत्रण कक्षाकडे पाठवण्यासाठी अ‍ॅपमधील लाल SOS बटण दाबा.",
        )

    return NO_DATA_TEXT


#: What the fallback says when nothing routes.  It lists what it *can* answer,
#: because "I did not understand" with no menu leaves a person tapping.
UNROUTED = (
    "I could not tell what you are asking. I can help with darshan timings, your pass and wait "
    "time, how crowded an area is, and where to find water, a toilet or a medical camp. "
    "For anything else, call 108 or ask a volunteer.",
    "तुम्ही काय विचारत आहात ते मला समजले नाही. दर्शनाच्या वेळा, तुमचा पास आणि प्रतीक्षा वेळ, "
    "एखाद्या भागातील गर्दी, आणि पाणी, स्वच्छतागृह अथवा वैद्यकीय शिबिर कुठे आहे — यात मी मदत करू शकतो. "
    "इतर कशासाठी १०८ वर फोन करा किंवा स्वयंसेवकाला विचारा.",
)


async def answer(ctx: ToolContext, question: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Answer without a model.  Returns (english, marathi, tool_log)."""
    routed = route(question)
    if routed is None:
        return (*UNROUTED, [])

    name, args = routed
    result, ms = await tools.call(ctx, name, args)
    english, marathi = render(result)
    return english, marathi, [result.as_log(ms)]
