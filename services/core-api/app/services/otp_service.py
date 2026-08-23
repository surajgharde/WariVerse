"""Phone OTP: issue, throttle, verify.

Codes live in Redis, hashed, with a TTL and an attempt counter.  Rate limit is
3 per hour per phone (Section 9).  In development `OTP_DEBUG_ECHO` returns the
code in the response so the flow is testable without an SMS gateway; the config
validator refuses to call an environment production-safe while it is on.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis_client import redis
from app.core.security import (
    constant_time_equals,
    generate_numeric_code,
    hash_phone,
    mask_phone,
    sha256_hex,
)

logger = get_logger(__name__)

_CODE_KEY = "otp:code:{phone_hash}"
_ATTEMPTS_KEY = "otp:attempts:{phone_hash}"
_QUOTA_KEY = "otp:quota:{phone_hash}"


@dataclass(frozen=True, slots=True)
class OtpIssue:
    phone_hash: str
    expires_in: int
    debug_code: str | None


def _digest(code: str, phone_hash: str) -> str:
    return sha256_hex(f"{phone_hash}:{code}".encode())


async def request_otp(phone: str, purpose: str = "login") -> OtpIssue:
    phone_hash = hash_phone(phone)
    quota_key = _QUOTA_KEY.format(phone_hash=phone_hash)

    used = await redis.incr(quota_key)
    if used == 1:
        await redis.expire(quota_key, 3600)
    if used > settings.otp_max_per_hour:
        ttl = await redis.ttl(quota_key)
        raise AppError(
            "RATE_LIMITED",
            details={"retry_after_seconds": max(ttl, 0), "limit": settings.otp_max_per_hour},
        )

    code = generate_numeric_code(settings.otp_length)
    await redis.setex(
        _CODE_KEY.format(phone_hash=phone_hash),
        settings.otp_ttl_seconds,
        _digest(code, phone_hash),
    )
    await redis.delete(_ATTEMPTS_KEY.format(phone_hash=phone_hash))

    logger.info(
        "otp_issued",
        extra={"phone": mask_phone(phone), "purpose": purpose, "ttl": settings.otp_ttl_seconds},
    )
    # TODO(phase-5): hand this to the notifier service for SMS delivery.
    # Until the gateway is wired, development echoes the code and production
    # refuses to boot with OTP_DEBUG_ECHO on.
    return OtpIssue(
        phone_hash=phone_hash,
        expires_in=settings.otp_ttl_seconds,
        debug_code=code if settings.otp_debug_echo else None,
    )


async def verify_otp(phone: str, code: str) -> str:
    """Return the phone hash on success; raise AppError otherwise."""
    phone_hash = hash_phone(phone)
    code_key = _CODE_KEY.format(phone_hash=phone_hash)
    attempts_key = _ATTEMPTS_KEY.format(phone_hash=phone_hash)

    stored = await redis.get(code_key)
    if stored is None:
        raise AppError("OTP_EXPIRED")

    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        await redis.expire(attempts_key, settings.otp_ttl_seconds)
    if attempts > settings.otp_max_attempts:
        await redis.delete(code_key)
        raise AppError("OTP_TOO_MANY_ATTEMPTS")

    if not constant_time_equals(stored, _digest(code.strip(), phone_hash)):
        raise AppError(
            "OTP_INVALID",
            details={"attempts_remaining": max(0, settings.otp_max_attempts - attempts)},
        )

    # Single use.
    await redis.delete(code_key, attempts_key)
    logger.info("otp_verified", extra={"phone": mask_phone(phone)})
    return phone_hash
