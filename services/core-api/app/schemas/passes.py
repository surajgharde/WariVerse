"""Slot, pass and checkpoint schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import Field, field_validator

from app.schemas.auth import _validate_phone
from app.schemas.common import ApiModel
from app.services.pass_service import ScanOutcome
from app.services.slot_service import MAX_GROUP_SIZE


class SlotOut(ApiModel):
    id: uuid.UUID
    date: date
    start_time: time
    end_time: time
    capacity: int
    booked_count: int
    # Surfaced so the UI can explain *why* a slot shows fewer seats than its
    # capacity — the reserve is a promise to walk-in pilgrims, not a hidden
    # quota (Section 5, E1).
    walkin_reserve: int
    available: int
    status: str
    gate_code: str | None = None
    is_bookable: bool


class SlotGrid(ApiModel):
    date: date
    slots: list[SlotOut]
    total_available: int
    walkin_reserve_pct: float
    generated_at: datetime


class PassMemberIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    age_band: str | None = Field(default=None, pattern="^(child|adult|senior)$")


class PassMemberOut(ApiModel):
    name: str
    age_band: str | None = None


class PassCreate(ApiModel):
    slot_id: uuid.UUID
    phone: str
    holder_name: str = Field(min_length=1, max_length=120)
    group_size: int = Field(default=1, ge=1, le=MAX_GROUP_SIZE)
    language: str = "mr"
    members: list[PassMemberIn] = Field(default_factory=list)
    # Opt-in only.  See `Pass.allow_early_reslot`.
    allow_early_reslot: bool = False

    _check_phone = field_validator("phone")(_validate_phone)

    @field_validator("members")
    @classmethod
    def _members_fit(cls, value: list[PassMemberIn]) -> list[PassMemberIn]:
        if len(value) > MAX_GROUP_SIZE:
            raise ValueError(f"A pass covers at most {MAX_GROUP_SIZE} people")
        return value


class PassOut(ApiModel):
    id: uuid.UUID
    reference: str
    status: str
    group_size: int
    holder_name: str
    slot_date: date
    slot_start: datetime
    slot_end: datetime
    gate_code: str | None = None
    issued_at: datetime
    scanned_at: datetime | None = None
    # Honest wait, not just the slot time: whichever is later, the slot opening
    # or clearing the queue actually ahead of them (Section 4/M1).
    estimated_entry_at: datetime
    queue_ahead: int
    reslot_count: int
    was_reslotted: bool
    allow_early_reslot: bool
    as_of: datetime


class PassIssued(PassOut):
    """Booking response — carries the QR seed, returned exactly once."""

    qr_secret: str = Field(description="Store on device; used to compute the rolling code offline.")
    qr_payload: str
    qr_valid_for_seconds: int


class QrOut(ApiModel):
    qr_payload: str
    valid_for_seconds: int
    rotates_every_seconds: int
    as_of: datetime


class DayKeyOut(ApiModel):
    """Public key a scanner caches to verify passes with no network."""

    date: date
    algorithm: str = "Ed25519"
    public_key_b64: str
    note: str = "Cache this to verify pass envelopes offline. Rotates daily."


class ScanRequest(ApiModel):
    qr_payload: str = Field(min_length=8, max_length=4096)
    gate_code: str | None = None
    # Set by a scanner replaying a queued offline scan, so the record shows when
    # it actually happened rather than when the network came back.
    scanned_at: datetime | None = None


class ScanResponse(ApiModel):
    outcome: ScanOutcome
    reason: str
    message_mr: str
    pass_reference: str | None = None
    group_size: int | None = None
    slot_start: datetime | None = None
    slot_end: datetime | None = None
    scanned_at: datetime | None = None
    minutes_early: int | None = None


class ScannerBundleOut(ApiModel):
    gate_code: str | None
    hours_ahead: int
    generated_at: datetime
    day_key: DayKeyOut
    passes: list[dict[str, object]]


class ReslotRunOut(ApiModel):
    ran_at: datetime
    planned: int
    actual: int
    deviation: float
    should_reslot: bool
    delay_minutes: int
    passes_moved: int
    reason: str
