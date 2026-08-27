"""Standard error envelope (Section 9).

Every error the API emits — ours, FastAPI validation, or an unhandled crash —
comes out in the same shape, with a Marathi message, because the pilgrim app is
Marathi-first and cannot be left rendering an English stack trace.

    {"error": {"code", "message", "message_mr", "details", "trace_id"}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_trace_id

logger = get_logger(__name__)

#: code -> (http status, English message, Marathi message)
ERROR_CATALOG: dict[str, tuple[int, str, str]] = {
    # generic
    "BAD_REQUEST": (400, "The request could not be processed.", "विनंतीवर प्रक्रिया करता आली नाही."),
    "VALIDATION_ERROR": (422, "Some fields are invalid.", "काही माहिती चुकीची आहे."),
    "NOT_FOUND": (404, "Not found.", "सापडले नाही."),
    "CONFLICT": (409, "This action conflicts with the current state.", "ही कृती सध्याच्या स्थितीशी जुळत नाही."),
    "INTERNAL_ERROR": (500, "Something went wrong at our end.", "आमच्याकडे काहीतरी बिघडले आहे."),
    "SERVICE_UNAVAILABLE": (503, "This service is temporarily unavailable.", "ही सेवा तात्पुरती बंद आहे."),
    # auth
    "UNAUTHENTICATED": (401, "Please sign in to continue.", "पुढे जाण्यासाठी कृपया साइन इन करा."),
    "INVALID_CREDENTIALS": (401, "Phone number or password is incorrect.", "फोन नंबर किंवा पासवर्ड चुकीचा आहे."),
    "TOKEN_EXPIRED": (401, "Your session has expired. Please sign in again.", "तुमचे सत्र संपले आहे. पुन्हा साइन इन करा."),
    "TOKEN_INVALID": (401, "Your session is not valid.", "तुमचे सत्र वैध नाही."),
    "TOKEN_REUSED": (
        401,
        "This session was ended for security reasons. Please sign in again.",
        "सुरक्षेच्या कारणास्तव हे सत्र बंद केले आहे. कृपया पुन्हा साइन इन करा.",
    ),
    "FORBIDDEN": (403, "You do not have permission to do this.", "हे करण्याची तुम्हाला परवानगी नाही."),
    "MFA_REQUIRED": (401, "Two-factor verification is required.", "दुहेरी पडताळणी आवश्यक आहे."),
    "MFA_INVALID": (401, "That verification code is not correct.", "तो पडताळणी कोड बरोबर नाही."),
    "ACCOUNT_DISABLED": (403, "This account is disabled.", "हे खाते बंद केले आहे."),
    # otp
    "OTP_INVALID": (400, "That code is not correct.", "तो कोड बरोबर नाही."),
    "OTP_EXPIRED": (400, "That code has expired. Request a new one.", "त्या कोडची मुदत संपली आहे. नवीन कोड मागवा."),
    "OTP_NOT_REQUESTED": (400, "Request a code first.", "आधी कोड मागवा."),
    "OTP_TOO_MANY_ATTEMPTS": (
        429,
        "Too many wrong attempts. Request a new code.",
        "खूप वेळा चुकीचा प्रयत्न. नवीन कोड मागवा.",
    ),
    # rate limiting
    "RATE_LIMITED": (429, "Too many requests. Please wait and try again.", "खूप विनंत्या. कृपया थोडे थांबा."),
    # passes (Phase 2)
    "SLOT_FULL": (409, "This time slot is full. Please choose another.", "ही वेळ भरली आहे. कृपया दुसरी वेळ निवडा."),
    "SLOT_CLOSED": (409, "Bookings for this slot are closed.", "या वेळेसाठी नोंदणी बंद आहे."),
    "PASS_NOT_FOUND": (404, "No pass found.", "पास सापडला नाही."),
    "PASS_ALREADY_USED": (409, "This pass has already been scanned.", "हा पास आधीच स्कॅन झाला आहे."),
    "PASS_EXPIRED": (409, "This pass has expired.", "या पासची मुदत संपली आहे."),
    "PASS_CANCELLED": (409, "This pass was cancelled.", "हा पास रद्द केला आहे."),
    "PASS_TOO_EARLY": (
        409,
        "It is not time for this slot yet. Please come at your slot time.",
        "या वेळेची अजून वेळ झालेली नाही. कृपया तुमच्या वेळेवर या.",
    ),
    "GROUP_TOO_LARGE": (400, "A pass covers up to 6 people.", "एका पासवर जास्तीत जास्त ६ जण."),
    "QR_INVALID": (400, "This code could not be read.", "हा कोड वाचता आला नाही."),
    "QR_STALE": (
        400,
        "This code is out of date. Ask the pilgrim to refresh their pass screen.",
        "हा कोड जुना आहे. यात्रेकरूला पास स्क्रीन पुन्हा उघडण्यास सांगा.",
    ),
    "SLOT_NOT_FOUND": (404, "That time slot does not exist.", "ती वेळ अस्तित्वात नाही."),
    "BOOKING_LIMIT_REACHED": (
        429,
        "You have booked the maximum number of passes for today.",
        "तुम्ही आजसाठी जास्तीत जास्त पास नोंदवले आहेत.",
    ),
    "DATE_OUT_OF_RANGE": (
        400,
        "Bookings are not open for that date.",
        "त्या तारखेसाठी नोंदणी सुरू नाही.",
    ),
    "GATE_NOT_FOUND": (404, "That gate is not configured.", "ते द्वार नोंदवलेले नाही."),
    # crowd (Phase 3)
    "ZONE_NOT_FOUND": (404, "That zone does not exist.", "तो विभाग अस्तित्वात नाही."),
    "CAMERA_NOT_FOUND": (404, "That camera is not configured.", "तो कॅमेरा नोंदवलेला नाही."),
    "ALERT_NOT_FOUND": (404, "That alert does not exist.", "ती सूचना अस्तित्वात नाही."),
    "ALERT_ALREADY_CLOSED": (
        409,
        "That alert has already been closed.",
        "ती सूचना आधीच बंद केली आहे.",
    ),
    "CALIBRATION_INVALID": (
        400,
        "These four points do not describe a valid ground plane. Pick points that are not in a straight line.",
        "हे चार बिंदू योग्य जमिनीचा नकाशा दर्शवत नाहीत. एका रेषेत नसलेले बिंदू निवडा.",
    ),
    "ZONE_NOT_CALIBRATED": (
        409,
        "This zone has no camera calibration, so its density figure would be fiction.",
        "या विभागाचे कॅमेरा मापन झालेले नाही, त्यामुळे गर्दीचा आकडा विश्वासार्ह नाही.",
    ),
    "READING_REJECTED": (
        422,
        "This crowd reading could not be accepted.",
        "ही गर्दीची नोंद स्वीकारता आली नाही.",
    ),
    "NO_CROWD_DATA": (
        503,
        "No crowd data is available right now. Treat the map as unknown, not as clear.",
        "सध्या गर्दीची माहिती उपलब्ध नाही. नकाशा 'माहिती नाही' समजा, 'मोकळे' नाही.",
    ),
    # incidents (Phase 5)
    # Note what is *not* in this block: there is no SOS_RATE_LIMITED. Section 9
    # says never hard-block an SOS, so raising one over the limit attaches to the
    # caller's existing open incident instead of failing. A person pressing the
    # button four times is frightened, not abusive.
    "INCIDENT_NOT_FOUND": (404, "That incident does not exist.", "ती घटना अस्तित्वात नाही."),
    "INCIDENT_CLOSED": (
        409,
        "That incident is already closed.",
        "ती घटना आधीच बंद केली आहे.",
    ),
    "INVALID_TRANSITION": (
        409,
        "An incident cannot move to that state from where it is now.",
        "घटनेची सध्याची स्थिती पाहता ती त्या स्थितीत नेता येणार नाही.",
    ),
    "OUTCOME_NOTE_REQUIRED": (
        400,
        "Closing an incident needs a note saying what was actually done.",
        "घटना बंद करताना प्रत्यक्षात काय केले याची नोंद आवश्यक आहे.",
    ),
    "RESPONDER_NOT_FOUND": (404, "That responder unit does not exist.", "ते पथक नोंदवलेले नाही."),
    "RESPONDER_UNAVAILABLE": (
        409,
        "That unit is not available. Choose another, or free it from its current incident first.",
        "ते पथक सध्या उपलब्ध नाही. दुसरे निवडा, किंवा त्याची सध्याची घटना आधी मोकळी करा.",
    ),
    "NO_RESPONDER_AVAILABLE": (
        503,
        "No unit of that type is available. Dispatch by radio and log it here afterwards.",
        "त्या प्रकारचे कोणतेही पथक उपलब्ध नाही. रेडिओवरून पाठवा आणि नंतर येथे नोंद करा.",
    ),
    "MISSING_PERSON_NOT_FOUND": (
        404,
        "That missing-person case does not exist.",
        "ती हरवलेल्या व्यक्तीची नोंद अस्तित्वात नाही.",
    ),
    "MISSING_PERSON_CLOSED": (
        409,
        "That case is already closed.",
        "ती नोंद आधीच बंद केली आहे.",
    ),
    # breach ledger (Phase 6)
    #
    # These messages are worded for a Security Officer, not a pilgrim — the
    # whole module is invisible below that role. They are still bilingual,
    # because the officer reading them at 2 a.m. in Pandharpur is as likely to
    # be working in Marathi as anyone else.
    "BREACH_NOT_FOUND": (404, "That breach record does not exist.", "ती उल्लंघनाची नोंद अस्तित्वात नाही."),
    "TRIPWIRE_NOT_FOUND": (404, "That tripwire is not configured.", "ती नियंत्रण-रेषा नोंदवलेली नाही."),
    "REVIEW_REASON_REQUIRED": (
        400,
        "Marking a record authorised or a false positive needs a written reason.",
        "नोंद 'परवानगी होती' किंवा 'चुकीची नोंद' म्हणून ठरवताना लेखी कारण आवश्यक आहे.",
    ),
    "DELETION_REASON_REQUIRED": (
        400,
        "Removing evidence needs a written reason. It is recorded permanently.",
        "पुरावा काढून टाकण्यासाठी लेखी कारण आवश्यक आहे. ते कायमस्वरूपी नोंदवले जाते.",
    ),
    "BREACH_REDACTED": (
        409,
        "That record's evidence has already been removed.",
        "त्या नोंदीचा पुरावा आधीच काढून टाकला आहे.",
    ),
    "CLIP_UNAVAILABLE": (
        404,
        "There is no clip on this record, or it has passed its retention period.",
        "या नोंदीवर चित्रफीत नाही, किंवा तिचा साठवण कालावधी संपला आहे.",
    ),
    "REAUTH_REQUIRED": (
        401,
        "Viewing evidence needs your password again. Every view is logged.",
        "पुरावा पाहण्यासाठी पुन्हा पासवर्ड आवश्यक आहे. प्रत्येक वेळी नोंद ठेवली जाते.",
    ),
    "CHAIN_BROKEN": (
        409,
        "The evidence chain does not verify. Do not act on this ledger until it is investigated.",
        "पुराव्याची साखळी तपासणीत टिकत नाही. तपास होईपर्यंत या नोंदवहीवर कारवाई करू नका.",
    ),
    # palkhi & dindi tracking (Phase 9, Section 4/M8)
    #
    # These are read by a volunteer on a road somewhere between Alandi and
    # Pandharpur, on a phone, in Marathi. The messages say what to do next
    # rather than what went wrong, because there is nobody beside them to ask.
    "DINDI_NOT_FOUND": (404, "That Dindi is not registered.", "ती दिंडी नोंदवलेली नाही."),
    "HALT_TOWN_NOT_FOUND": (404, "That halt town is not on the route.", "ते मुक्कामाचे गाव मार्गावर नाही."),
    "ROUTE_NOT_FOUND": (404, "That route does not exist.", "तो मार्ग अस्तित्वात नाही."),
    "DINDI_CODE_TAKEN": (
        409,
        "A Dindi with that code is already registered.",
        "त्या क्रमांकाची दिंडी आधीच नोंदवलेली आहे.",
    ),
    "DINDI_DEVICE_MISMATCH": (
        409,
        "This Dindi already has a designated tracking phone. Use that phone, or ask an administrator "
        "to move the registration to this one — two phones reporting for one Dindi puts it in two "
        "places on the halt towns' board.",
        "या दिंडीसाठी आधीच एक ट्रॅकिंग फोन नोंदवलेला आहे. तोच फोन वापरा, किंवा नोंदणी या फोनवर "
        "हलवण्यासाठी प्रशासकाला सांगा — एका दिंडीसाठी दोन फोन नोंदी पाठवल्यास ती मुक्कामाच्या "
        "गावांच्या यादीत दोन ठिकाणी दिसते.",
    ),
    "DINDI_INACTIVE": (
        409,
        "This Dindi is not walking. Ask an administrator to reactivate it before sending positions.",
        "ही दिंडी सध्या चालत नाही. नोंदी पाठवण्यापूर्वी प्रशासकाकडून ती पुन्हा सुरू करून घ्या.",
    ),
    "PING_INVALID": (
        400,
        "That position could not be read. Check that location is switched on and try again.",
        "ती जागा वाचता आली नाही. लोकेशन सुरू आहे का ते पाहा आणि पुन्हा प्रयत्न करा.",
    ),
    "SCHEDULE_INVALID": (
        400,
        "A halt schedule must list each town once, in walking order, with arrival times that move forward.",
        "मुक्कामाच्या वेळापत्रकात प्रत्येक गाव एकदाच, चालण्याच्या क्रमाने आणि पुढे जाणाऱ्या वेळांसह हवे.",
    ),
    # assistant (Phase 9, Section 13)
    "ASSISTANT_DISABLED": (
        503,
        "The assistant is switched off. The information you need is still on the main screens, "
        "and the helpline number is on the home page.",
        "सहाय्यक सध्या बंद आहे. तुम्हाला हवी असलेली माहिती मुख्य स्क्रीनवर आहे, आणि मदत क्रमांक "
        "मुख्यपृष्ठावर आहे.",
    ),
    "ASSISTANT_RATE_LIMITED": (
        429,
        "That is a lot of questions in a short time. Please wait a minute.",
        "थोड्या वेळात खूप प्रश्न विचारले आहेत. कृपया एक मिनिट थांबा.",
    ),
}

_DEFAULT = ("INTERNAL_ERROR", 500)


class AppError(Exception):
    """Raise this anywhere; the handler turns it into the envelope."""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        message_mr: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        catalog_status, catalog_msg, catalog_msg_mr = ERROR_CATALOG.get(
            code, (500, "Something went wrong at our end.", "आमच्याकडे काहीतरी बिघडले आहे.")
        )
        self.code = code
        self.status_code = status_code or catalog_status
        self.message = message or catalog_msg
        self.message_mr = message_mr or catalog_msg_mr
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")


def error_body(
    code: str,
    message: str,
    message_mr: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "message_mr": message_mr,
            "details": details or {},
            "trace_id": get_trace_id(),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", extra={"code": exc.code, "details": exc.details})
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.message_mr, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        code = "VALIDATION_ERROR"
        _, msg, msg_mr = ERROR_CATALOG[code]
        fields = [
            {"field": ".".join(str(p) for p in err.get("loc", ())[1:]), "reason": err.get("msg", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(code, msg, msg_mr, {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            400: "BAD_REQUEST",
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            429: "RATE_LIMITED",
            503: "SERVICE_UNAVAILABLE",
        }.get(exc.status_code, "INTERNAL_ERROR")
        _, msg, msg_mr = ERROR_CATALOG[code]
        detail = exc.detail if isinstance(exc.detail, str) else None
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, detail or msg, msg_mr),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", extra={"exc_type": type(exc).__name__})
        code, http_status = _DEFAULT
        _, msg, msg_mr = ERROR_CATALOG[code]
        return JSONResponse(status_code=http_status, content=error_body(code, msg, msg_mr))
