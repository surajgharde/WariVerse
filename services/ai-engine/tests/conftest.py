"""Fixtures.

None of these tests need Postgres, Redis, a camera or the vision stack.  That is
the point of the `sim` default: the crowd pipeline is fully testable on a laptop
with `pip install -r requirements-dev.txt`.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "CROWD_SOURCE": "sim",
        "AI_SERVICE_TOKEN": "test-ai-service-token",
        "CORE_API_URL": "http://core-api.invalid",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402

from app.models import ZoneSpec  # noqa: E402

#: Roughly the Pandharpur seed, so the numbers in these tests mean something.
ZONES = [
    ZoneSpec("11111111-1111-1111-1111-111111111111", "TC", "Temple Core", 1200.0, 2400, "temple_core"),
    ZoneSpec("22222222-2222-2222-2222-222222222222", "QC", "Queue Corridor", 4800.0, 12000, "queue"),
    ZoneSpec("33333333-3333-3333-3333-333333333333", "CG", "Chandrabhaga Ghat", 15000.0, 30000, "ghat"),
    ZoneSpec("44444444-4444-4444-4444-444444444444", "NW", "Gate Plaza", 2600.0, 6000, "corridor"),
]


@pytest.fixture
def zones() -> list[ZoneSpec]:
    return list(ZONES)


@pytest.fixture
def ordinary_evening() -> datetime:
    """A busy but unremarkable Wari evening, six weeks before Ekadashi."""
    return datetime(2026, 6, 10, 17, 30, tzinfo=UTC)


@pytest.fixture
def ekadashi_morning() -> datetime:
    """Ashadhi Ekadashi, 05:30. The worst half hour of the year."""
    return datetime(2026, 7, 25, 5, 30, tzinfo=UTC)
