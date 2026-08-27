"""All ORM models.

Imported as one module so Alembic's autogenerate and `Base.metadata` see every
table without hunting for imports.
"""

from app.models.accessibility import (
    ASSISTANCE_SLA_MINUTES,
    AccessibilityProfile,
    AssistanceNeed,
    AssistanceRequest,
    RequestStatus,
)
from app.models.assistant import AssistantTurn, TurnOutcome
from app.models.audit import DEFAULT_CONFIG, AuditLog, SystemConfig
from app.models.base import Base, TimestampMixin
from app.models.breach import BreachEvent, ClipAccessLog, PurgeLog, ReviewStatus
from app.models.crowd import (
    DENSITY_THRESHOLDS,
    FORECAST_HORIZONS,
    Alert,
    AlertSeverity,
    AlertStatus,
    DensityLevel,
    DensityReading,
    Forecast,
    classify_density,
)
from app.models.geo import Camera, Facility, Gate, Tripwire, Zone
from app.models.heritage import HeritageItem, HeritageKind, ReviewState
from app.models.incidents import (
    SLA_MINUTES,
    Incident,
    IncidentEvent,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    MissingPerson,
    Responder,
)
from app.models.lostfound import (
    ItemCategory,
    LostFoundItem,
    LostFoundKind,
    LostFoundMatch,
    LostFoundStatus,
)
from app.models.palkhi import (
    Dindi,
    DindiPing,
    DindiScheduleStop,
    DindiStatus,
    HaltReadiness,
    HaltTown,
    Route,
)
from app.models.passes import (
    MAX_GROUP_SIZE,
    Pass,
    PassMember,
    PassNotification,
    PassStatus,
    Slot,
    SlotStatus,
)
from app.models.user import ContactSecret, User

__all__ = [
    "ASSISTANCE_SLA_MINUTES",
    "DEFAULT_CONFIG",
    "DENSITY_THRESHOLDS",
    "FORECAST_HORIZONS",
    "MAX_GROUP_SIZE",
    "SLA_MINUTES",
    "AccessibilityProfile",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
    "AssistanceNeed",
    "AssistanceRequest",
    "AssistantTurn",
    "AuditLog",
    "Base",
    "BreachEvent",
    "Camera",
    "ClipAccessLog",
    "ContactSecret",
    "DensityLevel",
    "DensityReading",
    "Dindi",
    "DindiPing",
    "DindiScheduleStop",
    "DindiStatus",
    "Facility",
    "Forecast",
    "Gate",
    "HeritageItem",
    "HeritageKind",
    "HaltReadiness",
    "HaltTown",
    "Incident",
    "IncidentEvent",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentType",
    "ItemCategory",
    "LostFoundItem",
    "LostFoundKind",
    "LostFoundMatch",
    "LostFoundStatus",
    "MissingPerson",
    "Pass",
    "PassMember",
    "PassNotification",
    "PassStatus",
    "PurgeLog",
    "RequestStatus",
    "ReviewState",
    "Responder",
    "ReviewStatus",
    "Route",
    "Slot",
    "SlotStatus",
    "SystemConfig",
    "TimestampMixin",
    "Tripwire",
    "TurnOutcome",
    "User",
    "Zone",
    "classify_density",
]
