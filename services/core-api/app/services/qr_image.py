"""Render a QR payload as inline SVG.

This exists for exactly one screen: the no-JavaScript pass card (Section 4/M7's
"fully usable without JS for the pass QR").  Everywhere else the pilgrim's own
device draws the QR, because the rolling code has to be recomputed every sixty
seconds and a device that must ask the server for a picture is a device that
stops working the moment it loses signal.

Why a library and not our own encoder: QR is a closed, published specification
with well-known tables, and a hand-rolled encoder with one wrong entry in the
error-correction block table produces codes that *look* right and fail to scan.
That failure would land at a gate, on a 65-year-old's phone, in a queue.  Segno
is pure Python, has no dependencies of its own, and is the boring choice.

Error correction is level M rather than L: the surface being scanned is a
cracked phone screen at an angle in daylight, and the extra ~10% of modules
buys back more than it costs.
"""

from __future__ import annotations

import io
from functools import lru_cache

import segno

#: Quiet zone in modules.  The specification's minimum is 4; less than that and
#: some scanners cannot find the finder patterns against a dark page.
QUIET_ZONE = 4

ERROR_LEVEL = "m"


@lru_cache(maxsize=256)
def svg(payload: str, *, scale: int = 8) -> str:
    """Return a standalone `<svg>` element for `payload`.

    No XML declaration and no doctype, so the caller can drop it straight into a
    page.  Black on transparent — the card's own background shows through, and a
    printed card stays scannable.

    Cached because the envelope half of a pass QR is stable for the pass's whole
    life and the rolling half changes once a minute: a pilgrim reloading the
    card page every 55 seconds hits at most a handful of distinct payloads.
    """
    code = segno.make(payload, error=ERROR_LEVEL, mode="byte", encoding="utf-8")
    buffer = io.BytesIO()
    code.save(
        buffer,
        kind="svg",
        scale=scale,
        border=QUIET_ZONE,
        dark="#16181D",
        light=None,
        xmldecl=False,
        svgns=True,
        omitsize=True,
        svgclass=None,
        lineclass=None,
    )
    return buffer.getvalue().decode("utf-8")


def version_of(payload: str) -> int:
    """The QR version the payload needs — used by tests to catch payload growth.

    A pass envelope that grows past what a phone camera can resolve at arm's
    length is a real regression, and it is invisible until someone tries to
    scan one.
    """
    return int(segno.make(payload, error=ERROR_LEVEL, mode="byte", encoding="utf-8").version)
