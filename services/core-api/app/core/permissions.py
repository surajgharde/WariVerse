"""Role/permission matrix (Section 2).

Authorisation lives in exactly one place.  Routes ask for a *permission*, never
for a role — so `if role == "admin"` never appears anywhere else in this
codebase.  Adding a role is editing one dict, not auditing fifty routes.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PILGRIM = "pilgrim"
    VOLUNTEER = "volunteer"
    SECURITY_OFFICER = "security_officer"
    RESPONDER = "responder"
    ADMINISTRATOR = "administrator"
    SYSTEM_ADMIN = "system_admin"


#: Roles that must complete MFA before privileged access (Section 12).
MFA_REQUIRED_ROLES: frozenset[Role] = frozenset({Role.ADMINISTRATOR, Role.SYSTEM_ADMIN})

#: Roles that authenticate with a password.  Pilgrims use phone OTP only.
PASSWORD_LOGIN_ROLES: frozenset[Role] = frozenset(
    {Role.VOLUNTEER, Role.SECURITY_OFFICER, Role.RESPONDER, Role.ADMINISTRATOR, Role.SYSTEM_ADMIN}
)


class Permission(StrEnum):
    # passes
    PASS_BOOK = "pass:book"
    PASS_VIEW_OWN = "pass:view_own"
    PASS_CANCEL_OWN = "pass:cancel_own"
    PASS_SCAN = "pass:scan"
    PASS_ADMIN = "pass:admin"  # quota + capacity control

    # crowd
    CROWD_VIEW_PUBLIC = "crowd:view_public"  # coarse zone colour, pilgrim-facing
    CROWD_VIEW_DETAIL = "crowd:view_detail"  # raw density, flow, per-camera
    CROWD_CALIBRATE = "crowd:calibrate"  # homography + zone geometry

    # alerts
    ALERT_VIEW = "alert:view"
    ALERT_ACKNOWLEDGE = "alert:acknowledge"

    # incidents
    INCIDENT_REPORT = "incident:report"
    INCIDENT_VIEW = "incident:view"
    INCIDENT_UPDATE_LOW = "incident:update_low"  # ack/close low severity
    INCIDENT_UPDATE_ANY = "incident:update_any"
    INCIDENT_DISPATCH = "incident:dispatch"
    SOS_RAISE = "sos:raise"

    # breach audit — Security Officer and Administrator only (Section 4/M5)
    BREACH_VIEW = "breach:view"
    BREACH_REVIEW = "breach:review"
    BREACH_CLIP_VIEW = "breach:clip_view"
    BREACH_DELETE = "breach:delete"  # System Admin only, reason mandatory

    # palkhi
    DINDI_VIEW = "dindi:view"
    DINDI_PING = "dindi:ping"
    DINDI_MANAGE = "dindi:manage"

    # analytics & admin
    ANALYTICS_VIEW = "analytics:view"
    ANALYTICS_EXPORT = "analytics:export"
    AUDIT_VIEW = "audit:view"
    USER_MANAGE = "user:manage"
    CAMERA_MANAGE = "camera:manage"
    ZONE_MANAGE = "zone:manage"
    CONFIG_MANAGE = "config:manage"


_PILGRIM: frozenset[Permission] = frozenset(
    {
        Permission.PASS_BOOK,
        Permission.PASS_VIEW_OWN,
        Permission.PASS_CANCEL_OWN,
        Permission.CROWD_VIEW_PUBLIC,
        Permission.INCIDENT_REPORT,
        Permission.SOS_RAISE,
        Permission.DINDI_VIEW,
    }
)

_VOLUNTEER: frozenset[Permission] = _PILGRIM | {
    Permission.PASS_SCAN,
    Permission.CROWD_VIEW_DETAIL,
    Permission.ALERT_VIEW,
    Permission.INCIDENT_VIEW,
    Permission.INCIDENT_UPDATE_LOW,
    Permission.DINDI_PING,
}

_SECURITY_OFFICER: frozenset[Permission] = _VOLUNTEER | {
    Permission.ALERT_ACKNOWLEDGE,
    Permission.INCIDENT_UPDATE_ANY,
    Permission.INCIDENT_DISPATCH,
    Permission.BREACH_VIEW,
    Permission.BREACH_REVIEW,
    Permission.BREACH_CLIP_VIEW,
}

_RESPONDER: frozenset[Permission] = _PILGRIM | {
    Permission.ALERT_VIEW,
    Permission.INCIDENT_VIEW,
    Permission.INCIDENT_UPDATE_ANY,
    Permission.CROWD_VIEW_DETAIL,
}

_ADMINISTRATOR: frozenset[Permission] = _SECURITY_OFFICER | {
    Permission.PASS_ADMIN,
    Permission.CROWD_CALIBRATE,
    Permission.ANALYTICS_VIEW,
    Permission.ANALYTICS_EXPORT,
    Permission.AUDIT_VIEW,
    Permission.DINDI_MANAGE,
    Permission.CAMERA_MANAGE,
    Permission.ZONE_MANAGE,
    Permission.CONFIG_MANAGE,
}

# System Admin gets everything, including the one permission Administrator is
# deliberately denied: deleting breach evidence.
_SYSTEM_ADMIN: frozenset[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.PILGRIM: _PILGRIM,
    Role.VOLUNTEER: _VOLUNTEER,
    Role.SECURITY_OFFICER: _SECURITY_OFFICER,
    Role.RESPONDER: _RESPONDER,
    Role.ADMINISTRATOR: _ADMINISTRATOR,
    Role.SYSTEM_ADMIN: _SYSTEM_ADMIN,
}

#: Actions that must always land in the audit log (Section 2).
PRIVILEGED_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.PASS_ADMIN,
        Permission.PASS_SCAN,
        Permission.CROWD_CALIBRATE,
        Permission.ALERT_ACKNOWLEDGE,
        Permission.INCIDENT_DISPATCH,
        Permission.INCIDENT_UPDATE_ANY,
        Permission.BREACH_VIEW,
        Permission.BREACH_REVIEW,
        Permission.BREACH_CLIP_VIEW,
        Permission.BREACH_DELETE,
        Permission.ANALYTICS_EXPORT,
        Permission.AUDIT_VIEW,
        Permission.USER_MANAGE,
        Permission.CAMERA_MANAGE,
        Permission.ZONE_MANAGE,
        Permission.CONFIG_MANAGE,
        Permission.DINDI_MANAGE,
    }
)


def permissions_for(role: Role | str) -> frozenset[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()


def has_permission(role: Role | str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def requires_mfa(role: Role | str) -> bool:
    try:
        return Role(role) in MFA_REQUIRED_ROLES
    except ValueError:
        return False
