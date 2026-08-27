"""Storing and reading crowd forecasts (Phase 8, Section 4/M6).

The core API does not *make* forecasts.  The AI engine does, because that is
where the model and the feature history live, and it publishes them through
`POST /ingest/forecast` like every other thing it learns.  This module is the
state side of that boundary: it writes the claim down, and it answers "what is
the current prediction for this zone" honestly, including when the answer is
"there isn't one".

Two rules shape everything here.

**A stale forecast is not a forecast.**  A 30-minute prediction issued 45
minutes ago describes a moment that has already passed.  It is not a slightly
worse forecast; it is a statement about the past being rendered as the future.
`latest()` drops those and names the zone in `unavailable_zones` instead.

**Provenance travels with the number.**  `model_version` and `trained_on` are
columns, not metadata, and every read path carries them out to the UI.  While
the model is trained on the simulation engine's generated season, every consumer
is told so in words.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Forecast, Zone, classify_density
from app.services import config_service

logger = get_logger(__name__)

#: Shown while `trained_on` is `simulated`.  Section 4/M6's cold-start rule
#: says to label it "in the UI"; the label is served from here so the console
#: and the pilgrim app cannot word it differently or forget it independently.
SIMULATED_NOTICE = (
    "This forecast comes from a model trained on simulated data. No season of real "
    "telemetry exists yet. Treat it as a shape, not as a number."
)
SIMULATED_NOTICE_MR = (
    "हा अंदाज सिम्युलेशनवर प्रशिक्षित मॉडेलचा आहे. प्रत्यक्ष हंगामाची माहिती अजून उपलब्ध नाही. "
    "हा आकडा नव्हे, कल समजा."
)


@dataclass(frozen=True, slots=True)
class ForecastIn:
    """One prediction, already resolved to a real zone."""

    zone_id: uuid.UUID
    horizon_minutes: int
    predicted_density: float
    interval_low: float
    interval_high: float
    model_version: str
    trained_on: str
    validation_mae: float | None


@dataclass(frozen=True, slots=True)
class ForecastView:
    """A stored forecast joined to its zone, with its age computed once."""

    zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    zone_name_mr: str
    horizon_minutes: int
    issued_at: datetime
    target_at: datetime
    predicted_density: float
    predicted_level: str
    interval_low: float
    interval_high: float
    model_version: str
    trained_on: str
    validation_mae: float | None
    age_seconds: float
    is_stale: bool


async def record(session: AsyncSession, issued_at: datetime, items: list[ForecastIn]) -> int:
    """Persist a batch of predictions issued at one moment.

    Upserted on the primary key rather than inserted.  A publisher that retries
    after a timeout it did not see resolve would otherwise fail the whole batch
    on a duplicate key, and drop 40 zones of forecast over one ambiguous network
    event.  Re-publishing the same issue time is idempotent, which is what a
    retrying client needs it to be.
    """
    if not items:
        return 0

    rows = [
        {
            "issued_at": issued_at,
            "zone_id": item.zone_id,
            "horizon_minutes": item.horizon_minutes,
            "target_at": issued_at + timedelta(minutes=item.horizon_minutes),
            "predicted_density": item.predicted_density,
            "predicted_level": str(classify_density(item.predicted_density)),
            "interval_low": item.interval_low,
            "interval_high": item.interval_high,
            "model_version": item.model_version,
            "trained_on": item.trained_on,
            "validation_mae": item.validation_mae,
        }
        for item in items
    ]

    statement = pg_insert(Forecast).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["issued_at", "zone_id", "horizon_minutes"],
            set_={
                "target_at": statement.excluded.target_at,
                "predicted_density": statement.excluded.predicted_density,
                "predicted_level": statement.excluded.predicted_level,
                "interval_low": statement.excluded.interval_low,
                "interval_high": statement.excluded.interval_high,
                "model_version": statement.excluded.model_version,
                "trained_on": statement.excluded.trained_on,
                "validation_mae": statement.excluded.validation_mae,
            },
        )
    )
    return len(rows)


async def latest(
    session: AsyncSession,
    *,
    zone_codes: list[str] | None = None,
    horizons: list[int] | None = None,
    at: datetime | None = None,
) -> tuple[list[ForecastView], list[str]]:
    """The newest live forecast per (zone, horizon), and the zones that have none.

    Returns the pair deliberately.  A caller that only got the list would have
    to reconstruct "which zones are missing" by differencing against the zone
    table, and the caller that forgets renders a short list as a calm one.
    """
    moment = at or now_utc()
    stale_after = await config_service.get_int(session, "forecast_stale_seconds")
    cutoff = moment - timedelta(seconds=stale_after)

    zone_rows = await session.execute(select(Zone).where(Zone.is_active.is_(True)))
    zones = {z.id: z for z in zone_rows.scalars()}
    if zone_codes:
        wanted = {code.upper() for code in zone_codes}
        zones = {zid: z for zid, z in zones.items() if z.code in wanted}
    if not zones:
        return [], []

    # DISTINCT ON gives the newest issue per (zone, horizon) in one pass; the
    # index created in 0006 is ordered to match, so this is a scan of the live
    # chunk rather than a sort of the retention window.
    statement = (
        select(Forecast)
        .where(Forecast.zone_id.in_(zones.keys()), Forecast.issued_at >= cutoff)
        .order_by(Forecast.zone_id, Forecast.horizon_minutes, Forecast.issued_at.desc())
        .distinct(Forecast.zone_id, Forecast.horizon_minutes)
    )
    if horizons:
        statement = statement.where(Forecast.horizon_minutes.in_(horizons))

    rows = await session.execute(statement)

    views: list[ForecastView] = []
    covered: set[uuid.UUID] = set()
    for row in rows.scalars():
        zone = zones.get(row.zone_id)
        if zone is None:  # pragma: no cover - filtered above
            continue
        age = (moment - row.issued_at).total_seconds()
        # A forecast whose target moment has already passed is spent, whatever
        # its age says. The two differ when the publisher stalls for less than
        # the staleness window but longer than the shortest horizon.
        spent = row.target_at <= moment
        if spent:
            continue
        covered.add(row.zone_id)
        views.append(
            ForecastView(
                zone_id=row.zone_id,
                zone_code=zone.code,
                zone_name=zone.name,
                zone_name_mr=zone.name_mr,
                horizon_minutes=row.horizon_minutes,
                issued_at=row.issued_at,
                target_at=row.target_at,
                predicted_density=row.predicted_density,
                predicted_level=row.predicted_level,
                interval_low=row.interval_low,
                interval_high=row.interval_high,
                model_version=row.model_version,
                trained_on=row.trained_on,
                validation_mae=row.validation_mae,
                age_seconds=round(age, 1),
                is_stale=False,
            )
        )

    views.sort(key=lambda v: (v.zone_code, v.horizon_minutes))
    unavailable = sorted(z.code for zid, z in zones.items() if zid not in covered)
    return views, unavailable


def provenance_notice(views: list[ForecastView]) -> tuple[str | None, str | None]:
    """The cold-start banner, or nothing once the models are on real data.

    Any simulated model in the set is enough to raise it.  A strip that is half
    real and half invented is one an operator will read as entirely real unless
    told otherwise, and the safe direction to be wrong in is the cautious one.
    """
    if any(v.trained_on == "simulated" for v in views):
        return SIMULATED_NOTICE, SIMULATED_NOTICE_MR
    return None, None


async def purge(session: AsyncSession, at: datetime | None = None) -> int:
    """Drop forecasts past the retention window.

    Kept far longer than they are useful as predictions, because their value
    after the fact is different: a forecast and the reading that eventually
    arrived for the same minute are the only way to say what the model's error
    actually was in the field, and a documented MAE is what Section 4/M6 asks
    the model to ship with.
    """
    moment = at or now_utc()
    days = await config_service.get_int(session, "forecast_retention_days")
    cutoff = moment - timedelta(days=days)
    result = await session.execute(delete(Forecast).where(Forecast.issued_at < cutoff))
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("forecast_purged", extra={"removed": removed, "older_than": cutoff.isoformat()})
    return removed
