"""Predicted crowd density (Section 9 `GET /forecast`, Section 4/M6).

One endpoint, read-only, behind `crowd:view_detail`.

Why a forecast is *not* public.  `/crowd/public` gives a pilgrim the colour of
the zone they are standing in, which is a fact about now that they can also see
with their eyes.  A published prediction is different: it is a map of where the
system expects the crowd to be in an hour, and handing that to every phone in
Pandharpur is both a crowd-steering instrument nobody has agreed to and a way to
produce the surge it predicted.  Operators get it; the pilgrim app gets the
advice that comes out of it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models.crowd import FORECAST_HORIZONS
from app.schemas.common import ErrorResponse
from app.schemas.crowd import ForecastOut, ForecastSeries
from app.services import forecast_service

router = APIRouter(tags=["forecast"], responses={404: {"model": ErrorResponse}})


@router.get("/forecast", response_model=ForecastSeries)
async def get_forecast(
    zone: list[str] | None = Query(default=None, description="Zone codes; omit for every zone"),
    horizon: list[int] | None = Query(default=None, description="Minutes ahead; omit for all published horizons"),
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> ForecastSeries:
    """The current prediction per zone per horizon.

    Zones with no live forecast come back in `unavailable_zones` rather than as
    a row of zeros — the same rule the KPI strip and the density map already
    follow, and it matters more here, not less. A model that has not warmed up
    and a zone that is genuinely expected to stay calm produce identical numbers
    if you let the missing case render as 0.0 p/m².

    `provenance_notice` is populated for as long as any model in the response
    was trained on simulated data. Section 4/M6 requires that label to reach the
    UI, so it is served rather than left for the front end to remember.
    """
    views, unavailable = await forecast_service.latest(
        session,
        zone_codes=zone,
        horizons=horizon,
    )
    notice, notice_mr = forecast_service.provenance_notice(views)

    return ForecastSeries(
        items=[
            ForecastOut(
                zone_id=v.zone_id,
                zone_code=v.zone_code,
                zone_name=v.zone_name,
                zone_name_mr=v.zone_name_mr,
                horizon_minutes=v.horizon_minutes,
                issued_at=v.issued_at,
                target_at=v.target_at,
                predicted_density=round(v.predicted_density, 3),
                predicted_level=v.predicted_level,
                interval_low=round(v.interval_low, 3),
                interval_high=round(v.interval_high, 3),
                model_version=v.model_version,
                trained_on=v.trained_on,
                validation_mae=v.validation_mae,
                age_seconds=v.age_seconds,
                is_stale=v.is_stale,
            )
            for v in views
        ],
        unavailable_zones=unavailable,
        horizons=sorted({v.horizon_minutes for v in views}) or list(horizon or FORECAST_HORIZONS),
        provenance_notice=notice,
        provenance_notice_mr=notice_mr,
        generated_at=now_utc(),
    )
