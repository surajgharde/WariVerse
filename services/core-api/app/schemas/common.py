"""Shared response shapes.

Every number an operator sees carries `as_of` and `source` (Section 4/M3), so
`Observation` is the wrapper for anything measured rather than stated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.security import now_utc

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, ser_json_timedelta="float")


class ErrorDetail(ApiModel):
    code: str
    message: str
    message_mr: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ErrorResponse(ApiModel):
    """Documented on every route so the OpenAPI contract shows the envelope."""

    error: ErrorDetail


class Page(ApiModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class Observation(ApiModel, Generic[T]):
    """A measured value with its provenance attached.

    `is_stale` is computed server-side rather than left to each client, so the
    admin console and the pilgrim app can never disagree about whether a number
    is trustworthy.
    """

    value: T
    as_of: datetime
    source: str  # live | video | sim | manual | forecast
    confidence: float = 1.0
    is_stale: bool = False

    @classmethod
    def build(
        cls,
        value: T,
        *,
        as_of: datetime,
        source: str,
        confidence: float = 1.0,
    ) -> Observation[T]:
        age = (now_utc() - as_of).total_seconds()
        return cls(
            value=value,
            as_of=as_of,
            source=source,
            confidence=confidence,
            is_stale=age > settings.stale_reading_seconds,
        )


class Ack(ApiModel):
    ok: bool = True
    message: str | None = None
    message_mr: str | None = None
