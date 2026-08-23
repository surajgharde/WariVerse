"""Darshan pass QR codes (Section 4/M1).

A QR is two independent pieces joined by `~`:

    <envelope>~<rolling_code>

**Envelope** — a JWT signed with the day's Ed25519 private key, carrying the
pass identity, its slot window, group size and gate.  A scanner caches the day's
*public* key and verifies this with no network at all.  This is what stops a
forged or edited pass.

**Rolling code** — 8 digits derived from the pass's own secret on a 60-second
step (TOTP with SHA-256).  The holder's device recomputes it offline every
minute, so a screenshot forwarded on WhatsApp is stale within a minute.

Why two mechanisms: there is no such thing as an asymmetric rolling code.  If a
scanner can *verify* a rolling code offline it necessarily holds the same secret
that *generates* it.  So authenticity is asymmetric (every scanner, zero trust
needed) and freshness is symmetric (only scanners that synced the narrow window
of upcoming passes for their gate — see `pass_service.scanner_bundle`).  A
scanner with no bundle still verifies authenticity and single-use, which is the
majority of the protection.

Section 4/M1 says "HMAC signature ... rotating every 60s" and "the scanner app
caches the day's public key".  Those two sentences cannot describe one
mechanism; this implements both, each where it works.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import jwt
import pyotp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings
from app.core.errors import AppError

QR_PREFIX = "WV1"
QR_SEPARATOR = "~"
ROLLING_STEP_SECONDS = 60
ROLLING_DIGITS = 8
#: Accept one step either side — a cheap Android clock drifts, and a volunteer
#: scanning at the boundary of a minute must not see a false rejection.
ROLLING_DRIFT_STEPS = 1

_ISSUER = "wariverse-pass"


@dataclass(frozen=True, slots=True)
class EnvelopeClaims:
    pass_id: str
    reference: str
    slot_start: datetime
    slot_end: datetime
    group_size: int
    gate_code: str | None
    issued_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# day keys
# ---------------------------------------------------------------------------
def _derive_day_seed(day: date) -> bytes:
    """Deterministic Ed25519 seed for a date.

    Derived rather than stored so every API instance signs with the same key
    without a shared keystore, and so yesterday's key can still verify
    yesterday's passes during the overnight tail of the queue.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"wariverse-qr-day-key",
        info=day.isoformat().encode("ascii"),
    )
    return hkdf.derive(settings.qr_signing_secret.encode("utf-8"))


@lru_cache(maxsize=8)
def day_private_key(day: date) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_derive_day_seed(day))


def day_public_key(day: date) -> Ed25519PublicKey:
    return day_private_key(day).public_key()


def day_public_key_b64(day: date) -> str:
    """Base64 raw public key — what a scanner caches for offline verification."""
    raw = day_public_key(day).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _private_key_pem(day: date) -> bytes:
    return day_private_key(day).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_key_pem(day: date) -> bytes:
    return day_public_key(day).public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------
def mint_envelope(
    *,
    pass_id: str,
    reference: str,
    slot_start: datetime,
    slot_end: datetime,
    group_size: int,
    gate_code: str | None,
    issued_at: datetime,
    grace_minutes: int,
) -> str:
    """Sign a pass envelope with the private key for its slot's date."""
    expires_at = slot_end + timedelta(minutes=grace_minutes)
    payload = {
        "pid": pass_id,
        "ref": reference,
        "sls": int(slot_start.timestamp()),
        "sle": int(slot_end.timestamp()),
        "gs": group_size,
        "gate": gate_code,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": _ISSUER,
    }
    return jwt.encode(payload, _private_key_pem(slot_start.date()), algorithm="EdDSA")


def verify_envelope(envelope: str, *, days: list[date] | None = None) -> EnvelopeClaims:
    """Verify against the candidate day keys and return the claims.

    `days` defaults to yesterday/today/tomorrow so a pass issued for a 23:30
    slot still verifies at 00:10, and so a scanner near midnight UTC does not
    reject a valid pass.
    """
    candidates = days or _default_key_days()
    last_error: AppError | None = None

    for day in candidates:
        try:
            payload = jwt.decode(
                envelope,
                _public_key_pem(day),
                algorithms=["EdDSA"],
                issuer=_ISSUER,
                options={"require": ["exp", "iat", "pid"]},
            )
        except jwt.ExpiredSignatureError:
            # Signature was good, the pass is simply past its grace window.
            raise AppError("PASS_EXPIRED") from None
        except jwt.InvalidTokenError as exc:
            last_error = AppError("QR_INVALID", details={"reason": type(exc).__name__})
            continue

        return EnvelopeClaims(
            pass_id=str(payload["pid"]),
            reference=str(payload.get("ref", "")),
            slot_start=datetime.fromtimestamp(payload["sls"], tz=UTC),
            slot_end=datetime.fromtimestamp(payload["sle"], tz=UTC),
            group_size=int(payload["gs"]),
            gate_code=payload.get("gate"),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    raise last_error or AppError("QR_INVALID")


def _default_key_days() -> list[date]:
    today = datetime.now(tz=UTC).date()
    return [today, today - timedelta(days=1), today + timedelta(days=1)]


# ---------------------------------------------------------------------------
# rolling code
# ---------------------------------------------------------------------------
def new_pass_secret() -> str:
    """Base32 secret handed to the holder's device once, at booking."""
    return pyotp.random_base32()


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(secret, digits=ROLLING_DIGITS, interval=ROLLING_STEP_SECONDS, digest="sha256")


def rolling_code(secret: str, at: datetime | None = None) -> str:
    """The code valid for the 60-second window containing `at`."""
    moment = at or datetime.now(tz=UTC)
    return _totp(secret).at(moment)


def verify_rolling_code(secret: str, code: str, at: datetime | None = None) -> bool:
    moment = at or datetime.now(tz=UTC)
    if not code or not code.isdigit():
        return False
    return _totp(secret).verify(code, for_time=moment, valid_window=ROLLING_DRIFT_STEPS)


def seconds_until_rotation(at: datetime | None = None) -> int:
    """For the holder's screen: how long this code stays good."""
    moment = at or datetime.now(tz=UTC)
    return ROLLING_STEP_SECONDS - int(moment.timestamp()) % ROLLING_STEP_SECONDS


# ---------------------------------------------------------------------------
# wire format
# ---------------------------------------------------------------------------
def build_qr(envelope: str, code: str) -> str:
    return f"{QR_PREFIX}{QR_SEPARATOR}{envelope}{QR_SEPARATOR}{code}"


def parse_qr(text: str) -> tuple[str, str]:
    """Split a scanned string into (envelope, rolling_code)."""
    parts = (text or "").strip().split(QR_SEPARATOR)
    if len(parts) != 3 or parts[0] != QR_PREFIX or not parts[1] or not parts[2]:
        raise AppError("QR_INVALID", details={"reason": "malformed payload"})
    return parts[1], parts[2]
