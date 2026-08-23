"""All ORM models.

Imported as one module so Alembic's autogenerate and `Base.metadata` see every
table without hunting for imports.
"""

from app.models.audit import DEFAULT_CONFIG, AuditLog, SystemConfig
from app.models.base import Base, TimestampMixin
from app.models.breach import BreachEvent, ClipAccessLog, PurgeLog, ReviewStatus
from app.models.crowd import (
    DENSITY_THRESHOLDS,
    Alert,
    AlertSeverity,
    AlertStatus,
    DensityLevel,
    DensityReading,
    classify_density,
)
from app.models.geo import Camera, Facility, Gate, Tripwire, Zone
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
from app.models.palkhi import Dindi, DindiPing, HaltTown, Route
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
    "DEFAULT_CONFIG",
    "DENSITY_THRESHOLDS",
    "MAX_GROUP_SIZE",
    "SLA_MINUTES",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
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
    "Facility",
    "Gate",
    "HaltTown",
    "Incident",
    "IncidentEvent",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentType",
    "MissingPerson",
    "Pass",
    "PassMember",
    "PassNotification",
    "PassStatus",
    "PurgeLog",
    "Responder",
    "ReviewStatus",
    "Route",
    "Slot",
    "SlotStatus",
    "SystemConfig",
    "TimestampMixin",
    "Tripwire",
    "User",
    "Zone",
    "classify_density",
]
