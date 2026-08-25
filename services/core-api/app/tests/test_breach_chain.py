"""The hash chain's arithmetic (Section 4/M5).

Pure-function tests — no database, no Redis. These are the properties that make
the ledger worth having, and a property guarded only by an integration test is
one that stops being checked the first time somebody runs the suite without
Docker.

Everything here is a version of the same question: **can a record be changed
without the chain noticing?** The answer has to be no for every field, which is
why the parametrised test at the bottom walks all of them rather than spot-
checking two.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import breach_service
from app.services.breach_service import GENESIS_HASH, canonical_payload, chain_hash_for

OCCURRED = datetime(2026, 7, 12, 14, 22, 31, 500_000, tzinfo=UTC)
TRIPWIRE = uuid.UUID("11111111-1111-1111-1111-111111111111")
CAMERA = uuid.UUID("22222222-2222-2222-2222-222222222222")
GATE = uuid.UUID("33333333-3333-3333-3333-333333333333")


def payload(**overrides: object) -> dict[str, object]:
    # Typed loosely on purpose: the whole point of the parametrised test below
    # is to substitute a differently-typed value into each field in turn.
    base: dict[str, object] = {
        "tripwire_id": TRIPWIRE,
        "camera_id": CAMERA,
        "gate_id": GATE,
        "occurred_at": OCCURRED,
        "direction": "in",
        "crossing_count": 1,
        "confidence": 0.87,
        "clip_sha256": "a" * 64,
        "sequence": 1,
    }
    base.update(overrides)
    return canonical_payload(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_the_same_evidence_always_hashes_the_same():
    assert chain_hash_for(GENESIS_HASH, payload()) == chain_hash_for(GENESIS_HASH, payload())


def test_key_order_does_not_change_the_hash():
    """The hash is over canonical JSON with sorted keys.

    Without `sort_keys` the digest would depend on dict insertion order, and the
    chain would stop verifying the day somebody reordered a dict literal in
    `canonical_payload` — a diff that looks like formatting and is actually a
    ledger-wide invalidation.
    """
    original = payload()
    shuffled = dict(reversed(list(original.items())))

    assert list(original) != list(shuffled), "the fixture must actually be reordered"
    assert chain_hash_for(GENESIS_HASH, original) == chain_hash_for(GENESIS_HASH, shuffled)


def test_the_hash_is_a_sha256_hex_digest():
    digest = chain_hash_for(GENESIS_HASH, payload())
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# sensitivity — the whole point
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 2),
        ("tripwire_id", uuid.UUID("44444444-4444-4444-4444-444444444444")),
        ("camera_id", uuid.UUID("55555555-5555-5555-5555-555555555555")),
        ("gate_id", uuid.UUID("66666666-6666-6666-6666-666666666666")),
        ("occurred_at", OCCURRED + timedelta(microseconds=1)),
        ("direction", "out"),
        ("crossing_count", 2),
        ("confidence", 0.88),
        ("clip_sha256", "b" * 64),
    ],
)
def test_changing_any_evidence_field_changes_the_hash(field, value):
    """Every field in the payload is load-bearing.

    Parametrised rather than spot-checked on purpose: a field added to
    `canonical_payload` and forgotten here is a field somebody could edit
    without the chain noticing, and spot-checks are exactly how that gets
    missed.
    """
    assert chain_hash_for(GENESIS_HASH, payload()) != chain_hash_for(GENESIS_HASH, payload(**{field: value}))


def test_a_one_microsecond_shift_in_the_timestamp_is_visible():
    """Explicit because it is the field somebody would actually move.

    "It happened at 14:22, not 14:25" is the edit an inquiry is about, and a
    hash that rounded to the second would let it through.
    """
    a = chain_hash_for(GENESIS_HASH, payload())
    b = chain_hash_for(GENESIS_HASH, payload(occurred_at=OCCURRED + timedelta(microseconds=1)))
    assert a != b


def test_the_hash_depends_on_the_previous_record():
    """This is what makes it a chain rather than a list of checksums.

    Identical evidence at a different point in the chain hashes differently, so
    a record cannot be lifted out of one position and dropped into another.
    """
    same_evidence = payload()
    assert chain_hash_for(GENESIS_HASH, same_evidence) != chain_hash_for("f" * 64, same_evidence)


# ---------------------------------------------------------------------------
# what is deliberately *not* in the payload
# ---------------------------------------------------------------------------
def test_review_columns_are_not_hashed():
    """A record must be reviewable without breaking its own chain.

    If `review_status` were inside the hash, the first Security Officer to mark
    an event verified would invalidate every hash after it — which would make
    the chain fire constantly and therefore mean nothing.
    """
    body = payload()
    assert "review_status" not in body
    assert "reviewed_by" not in body
    assert "review_reason" not in body


def test_no_field_in_the_payload_could_identify_a_person():
    """Section 12, checked rather than asserted in prose.

    The ledger's claim is "an unauthorised entry occurred at Gate 3 at 14:22".
    A field named for a track, a face, a person or a pass would be a different
    claim, and this test fails if one appears.
    """
    body = payload()
    banned = {"track_id", "person_id", "face", "embedding", "identity", "name", "phone", "pass_id"}
    assert not (set(body) & banned)
    # Also guard against a nested structure smuggling one in.
    serialised = json.dumps(body)
    assert not any(word in serialised for word in ("track_id", "embedding", "face"))


# ---------------------------------------------------------------------------
# genesis
# ---------------------------------------------------------------------------
def test_genesis_hash_is_distinguishable_from_a_real_hash():
    """The first record's `prev_hash` must not look like a computed one.

    Sixty-four zeros is not the SHA-256 of anything we produce, so "this is the
    start of the ledger" and "this record's predecessor was removed" stay
    distinguishable — which is the difference between a healthy chain and a
    broken one.
    """
    assert GENESIS_HASH == "0" * 64
    assert len(GENESIS_HASH) == 64
    assert chain_hash_for(GENESIS_HASH, payload()) != GENESIS_HASH


# ---------------------------------------------------------------------------
# the cross-reference window
# ---------------------------------------------------------------------------
def test_the_pass_scan_window_is_the_thirty_seconds_the_spec_asks_for():
    """Section 4/M5: "was a valid pass scanned at this gate within ±30 seconds?"

    Pinned as a test rather than left as a constant because widening it is the
    obvious way to make an inconvenient backlog of breach events disappear.
    """
    assert breach_service.PASS_SCAN_WINDOW_SECONDS == 30


def test_a_short_chain_links_end_to_end():
    """Three records, built the way the service builds them."""
    hashes = []
    prev = GENESIS_HASH
    for sequence in (1, 2, 3):
        digest = chain_hash_for(prev, payload(sequence=sequence))
        hashes.append((prev, digest))
        prev = digest

    # Each record's prev_hash is the previous record's chain_hash.
    assert hashes[0][0] == GENESIS_HASH
    assert hashes[1][0] == hashes[0][1]
    assert hashes[2][0] == hashes[1][1]
    assert len({h for _, h in hashes}) == 3, "identical evidence at different positions must differ"
