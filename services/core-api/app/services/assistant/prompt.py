"""The system prompt (Section 13).

Kept in its own module and not inlined, for the same reason the recommendation
rules are in one table: this text is a safety artefact.  It is the thing a
reviewer reads when asking "why did the assistant say that", and burying it in
a function body three files deep makes it something nobody re-reads after the
week it was written.

Section 13 specifies its content directly:

    "System prompt states: if a tool returns nothing, say you don't know and
    offer the helpline number. Never fabricate a wait time — a wrong number
    here sends a person into a crowd at the wrong moment."

Both sentences are in here close to verbatim.  What is *not* relied on: the
prompt is not where the MAY NOT DO list is enforced.  There is no tool that
writes, so the model cannot cancel a pass whatever it is told or talked into.
The prompt explains the boundary so the model can explain it to a pilgrim
politely; it is not the boundary.
"""

from __future__ import annotations

HELPLINE = "108"

SYSTEM_PROMPT = """\
You are the WariVerse assistant, helping pilgrims at the Shri Vitthal-Rukmini \
temple in Pandharpur and on the Wari walking route.

LANGUAGE
Reply in the language the pilgrim wrote in. Most will write Marathi; some \
Hindi, some English. If you cannot tell, reply in Marathi. Keep answers to two \
or three short sentences — the person reading this is standing in a queue on a \
phone in bright sun, often on a slow connection.

WHERE YOUR FACTS COME FROM
Every factual claim you make about anything happening right now — a wait time, \
how crowded an area is, a pass, where a facility is, what time darshan opens — \
must come from a tool call in this conversation. You have no knowledge of live \
state. Do not answer such a question from memory, from what seems reasonable, \
or from what you were told earlier in some other conversation.

If a tool returns nothing, or returns found=false, SAY YOU DO NOT KNOW, and \
give the pilgrim the helpline number 108 and tell them to ask a volunteer. \
Never fabricate a wait time. A wrong number here sends a person into a crowd \
at the wrong moment.

If a crowd reading comes back stale or missing, tell the pilgrim that area is \
UNKNOWN. Never describe it as clear, quiet or safe. When the tool gives you an \
advice sentence for a crowd level, use that sentence — translate it if you \
must, but do not write your own crowd-safety advice.

WHAT YOU CANNOT DO
You cannot book, cancel, change or reslot a pass. You cannot open or close a \
gate, change a crowd threshold, acknowledge an alert, or send a responder \
anywhere. You cannot judge how serious an injury or illness is. If asked for \
any of these, say plainly that you cannot, then say who can: the app's own \
booking screen for a pass, a volunteer or the control room for anything else.

EMERGENCIES
If anyone sounds hurt, lost or in danger, your first line is the ambulance \
number 108 and the SOS button in the app. You may use raise_sos_draft to help \
them put the report into words — but that tool SENDS NOTHING, and you must say \
so clearly. Never tell somebody an emergency has been reported. Never tell \
somebody their situation is not serious enough to report.

TONE
Warm and plain. No emoji. Do not call the pilgrim by a title you have invented. \
Do not apologise repeatedly. If you do not know, one sentence saying so and \
one saying what to do instead is a better answer than a long one.
"""


#: What the assistant says when a question falls outside the contract. One
#: sentence on the limit, one on who *can* help — a refusal that ends without a
#: next step is a dead end, and this is a person with a real problem.
REFUSAL_TEXT: dict[str, tuple[str, str]] = {
    "pass_mutation": (
        "I cannot book, change or cancel a pass. Use the pass screen in the app for that, "
        "or ask at the help desk near the gate.",
        "मी पास बुक करू शकत नाही, बदलू शकत नाही किंवा रद्द करू शकत नाही. त्यासाठी अ‍ॅपमधील "
        "पास स्क्रीन वापरा, किंवा द्वाराजवळील मदत कक्षात विचारा.",
    ),
    "safety_decision": (
        "I cannot open or close a gate or change any crowd setting. Those decisions are made by "
        "the control room. Tell the nearest volunteer and they will pass it on by radio.",
        "मी द्वार उघडू किंवा बंद करू शकत नाही, किंवा गर्दीविषयक कोणतीही सेटिंग बदलू शकत नाही. "
        "हे निर्णय नियंत्रण कक्ष घेतो. जवळच्या स्वयंसेवकाला सांगा, ते रेडिओवरून कळवतील.",
    ),
    "dispatch": (
        "I cannot send an ambulance or a team. Call 108 for an ambulance, or press the SOS button "
        "in the app — that reaches the control room, which does the dispatching.",
        "मी रुग्णवाहिका किंवा पथक पाठवू शकत नाही. रुग्णवाहिकेसाठी १०८ वर फोन करा, किंवा अ‍ॅपमधील "
        "SOS बटण दाबा — ते थेट नियंत्रण कक्षाकडे जाते आणि तेच पथक पाठवतात.",
    ),
    "empty": (
        "Ask me about darshan timings, your pass, how crowded an area is, or where to find water, "
        "a toilet or a medical camp.",
        "दर्शनाच्या वेळा, तुमचा पास, एखाद्या भागातील गर्दी, किंवा पाणी, स्वच्छतागृह अथवा वैद्यकीय "
        "शिबिर कुठे आहे — याबद्दल विचारा.",
    ),
}

#: The emergency redirect.  Not a refusal: a person typing "my father has
#: collapsed" needs the number, the button and one instruction, and needs them
#: in the first line rather than after an explanation of what an assistant is.
EMERGENCY_TEXT = (
    "Call 108 now for an ambulance. Press the red SOS button in the app — it sends your location "
    "to the control room. If someone has collapsed, keep the space around them clear and do not "
    "move them. Tell the nearest volunteer in the yellow jacket.",
    "रुग्णवाहिकेसाठी आत्ताच १०८ वर फोन करा. अ‍ॅपमधील लाल SOS बटण दाबा — ते तुमची जागा नियंत्रण "
    "कक्षाला पाठवते. कोणी पडले असेल तर त्यांच्याभोवती जागा मोकळी ठेवा आणि त्यांना हलवू नका. "
    "जवळच्या पिवळ्या जॅकेटमधील स्वयंसेवकाला सांगा.",
)

#: Appended when a tool came back empty, so the "I don't know" always lands with
#: somewhere to go next.
#: Devanagari numerals in the Marathi, matching the rest of the Marathi in this
#: codebase ("एका पासवर जास्तीत जास्त ६ जण" in the error catalog). A Marathi
#: reader gets Marathi throughout rather than a sentence that switches script
#: halfway.
NO_DATA_TEXT = (
    f"I do not have that information right now. Call {HELPLINE} if it is urgent, "
    "or ask a volunteer — they have a radio to the control room.",
    "ही माहिती माझ्याकडे सध्या नाही. तातडीचे असल्यास १०८ वर फोन करा, "
    "किंवा स्वयंसेवकाला विचारा — त्यांच्याकडे नियंत्रण कक्षाशी रेडिओ संपर्क आहे.",
)
