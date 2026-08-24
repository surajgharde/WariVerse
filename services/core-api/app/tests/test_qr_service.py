"""QR envelope and rolling code — pure unit tests.

The threat model these encode:
  - a forged or edited pass must not verify (envelope signature)
  - a screenshot forwarded on WhatsApp must go stale within a minute (rolling)
  - a scanner with no network must still be able to verify authenticity
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import AppError
from app.services import qr_service

# Anchored to tomorrow, not a fixed date: the envelope carries a real `exp`, so
# a hardcoded 2026-07-15 would start failing the day the calendar passed it.
_BASE = (datetime.now(tz=UTC) + timedelta(days=1)).replace(
    hour=15, minute=30, second=0, microsecond=0
)
SLOT_START = _BASE
SLOT_END = _BASE + timedelta(minutes=30)


def mint(**overrides: object) -> str:
    kwargs: dict = {
        "pass_id": "8f14e45f-ceea-467a-9f0a-1b2c3d4e5f60",
        "reference": "WVACDEFGH",
        "slot_start": SLOT_START,
        "slot_end": SLOT_END,
        "group_size": 4,
        "gate_code": "G1",
        # A real pass is always issued in the past; a future iat is
        # correctly rejected as an immature token.
        "issued_at": datetime.now(tz=UTC) - timedelta(hours=1),
        "grace_minutes": 45,
    }
    kwargs.update(overrides)
    return qr_service.mint_envelope(**kwargs)


# --- day keys --------------------------------------------------------------
def test_day_key_is_deterministic_across_instances() -> None:
    """Three API replicas must sign with the same key without a keystore."""
    first = qr_service.day_public_key_b64(SLOT_START.date())
    second = qr_service.day_public_key_b64(SLOT_START.date())
    assert first == second


def test_day_keys_differ_between_days() -> None:
    today = qr_service.day_public_key_b64(SLOT_START.date())
    tomorrow = qr_service.day_public_key_b64(SLOT_START.date() + timedelta(days=1))
    assert today != tomorrow


def test_public_key_is_a_raw_ed25519_key() -> None:
    raw = base64.b64decode(qr_service.day_public_key_b64(SLOT_START.date()))
    assert len(raw) == 32


# --- envelope --------------------------------------------------------------
def test_envelope_round_trip_carries_the_pass_details() -> None:
    claims = qr_service.verify_envelope(mint(), days=[SLOT_START.date()])
    assert claims.reference == "WVACDEFGH"
    assert claims.group_size == 4
    assert claims.gate_code == "G1"
    assert claims.slot_start == SLOT_START
    assert claims.slot_end == SLOT_END


def test_envelope_verifies_offline_with_only_the_public_key() -> None:
    """No database, no network — exactly what a scanner at a gate has."""
    envelope = mint()
    claims = qr_service.verify_envelope(envelope, days=[SLOT_START.date()])
    assert claims.pass_id == "8f14e45f-ceea-467a-9f0a-1b2c3d4e5f60"


def test_a_tampered_envelope_is_rejected() -> None:
    envelope = mint()
    header, payload, signature = envelope.split(".")
    forged = f"{header}.{payload[:-4]}AAAA.{signature}"
    with pytest.raises(AppError) as exc:
        qr_service.verify_envelope(forged, days=[SLOT_START.date()])
    assert exc.value.code == "QR_INVALID"


def test_an_envelope_signed_with_another_day_key_is_rejected() -> None:
    envelope = mint()
    with pytest.raises(AppError) as exc:
        qr_service.verify_envelope(envelope, days=[SLOT_START.date() + timedelta(days=5)])
    assert exc.value.code == "QR_INVALID"


def test_envelope_expires_after_the_grace_window() -> None:
    """Signature still good; the pass is simply past its life."""
    stale = mint(
        slot_start=SLOT_START - timedelta(days=400),
        slot_end=SLOT_END - timedelta(days=400),
        issued_at=datetime.now(tz=UTC) - timedelta(days=400),
    )
    with pytest.raises(AppError) as exc:
        qr_service.verify_envelope(stale, days=[(SLOT_START - timedelta(days=400)).date()])
    assert exc.value.code == "PASS_EXPIRED"


def test_garbage_is_rejected_without_a_crash() -> None:
    for junk in ("", "not-a-jwt", "a.b.c"):
        with pytest.raises(AppError):
            qr_service.verify_envelope(junk, days=[SLOT_START.date()])


# --- rolling code ----------------------------------------------------------
def test_rolling_code_shape() -> None:
    code = qr_service.rolling_code(qr_service.new_pass_secret(), SLOT_START)
    assert code.isdigit()
    assert len(code) == qr_service.ROLLING_DIGITS == 8


def test_rolling_code_verifies_within_its_window() -> None:
    secret = qr_service.new_pass_secret()
    code = qr_service.rolling_code(secret, SLOT_START)
    assert qr_service.verify_rolling_code(secret, code, SLOT_START)


def test_a_forwarded_screenshot_goes_stale() -> None:
    """The WhatsApp case: a code captured at 15:30 must not open a gate at
    15:35."""
    secret = qr_service.new_pass_secret()
    code = qr_service.rolling_code(secret, SLOT_START)
    assert not qr_service.verify_rolling_code(secret, code, SLOT_START + timedelta(minutes=5))


def test_one_step_of_clock_drift_is_tolerated() -> None:
    """A cheap Android at a gate boundary must not see a false rejection."""
    secret = qr_service.new_pass_secret()
    code = qr_service.rolling_code(secret, SLOT_START)
    assert qr_service.verify_rolling_code(secret, code, SLOT_START + timedelta(seconds=60))
    assert qr_service.verify_rolling_code(secret, code, SLOT_START - timedelta(seconds=60))
    assert not qr_service.verify_rolling_code(secret, code, SLOT_START + timedelta(seconds=180))


def test_another_passs_code_does_not_verify() -> None:
    mine = qr_service.new_pass_secret()
    theirs = qr_service.new_pass_secret()
    code = qr_service.rolling_code(theirs, SLOT_START)
    assert not qr_service.verify_rolling_code(mine, code, SLOT_START)


def test_malformed_codes_are_rejected_quietly() -> None:
    secret = qr_service.new_pass_secret()
    for bad in ("", "abcdefgh", "12 34", "٣٤٥٦٧٨٩٠"):
        assert not qr_service.verify_rolling_code(secret, bad, SLOT_START)


def test_seconds_until_rotation_is_within_the_step() -> None:
    at = SLOT_START.replace(second=20)
    assert qr_service.seconds_until_rotation(at) == 40


# --- wire format -----------------------------------------------------------
def test_build_and_parse_round_trip() -> None:
    envelope = mint()
    code = "12345678"
    payload = qr_service.build_qr(envelope, code)
    assert payload.startswith("WV1~")
    assert qr_service.parse_qr(payload) == (envelope, code)


@pytest.mark.parametrize(
    "bad",
    ["", "WV1~onlytwo", "WV2~a~b", "a~b~c", "WV1~~123", "WV1~abc~"],
)
def test_malformed_payloads_are_rejected(bad: str) -> None:
    with pytest.raises(AppError) as exc:
        qr_service.parse_qr(bad)
    assert exc.value.code == "QR_INVALID"
