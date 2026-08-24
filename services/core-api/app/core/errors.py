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
