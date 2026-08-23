"""The permission matrix is a safety control, so it gets tested like one.

These are pure unit tests — no database, no network.  They encode the rules
Section 2 and Section 4/M5 state in prose, so a future edit that quietly widens
access fails here instead of in a temple.
"""

from __future__ import annotations

import pytest

from app.core.permissions import (
    MFA_REQUIRED_ROLES,
    PRIVILEGED_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    has_permission,
    permissions_for,
    requires_mfa,
)


def test_every_role_has_an_entry() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_security_officer_can_do_everything_a_volunteer_can() -> None:
    # Section 2: "Everything Volunteer can do", plus their own.
    assert permissions_for(Role.VOLUNTEER) < permissions_for(Role.SECURITY_OFFICER)


def test_administrator_supersets_security_officer() -> None:
    assert permissions_for(Role.SECURITY_OFFICER) < permissions_for(Role.ADMINISTRATOR)


def test_system_admin_holds_every_permission() -> None:
    assert permissions_for(Role.SYSTEM_ADMIN) == frozenset(Permission)


@pytest.mark.parametrize(
    "role",
    [Role.PILGRIM, Role.VOLUNTEER, Role.RESPONDER],
)
def test_breach_records_are_invisible_below_security_officer(role: Role) -> None:
    # Section 4/M5: visible only to Security Officer and Administrator, never
    # on any pilgrim-facing surface.
    assert not has_permission(role, Permission.BREACH_VIEW)
    assert not has_permission(role, Permission.BREACH_REVIEW)
    assert not has_permission(role, Permission.BREACH_CLIP_VIEW)


def test_only_system_admin_can_delete_breach_evidence() -> None:
    # Section 4/M5: "Deletion is impossible for any role except System Admin."
    holders = [role for role in Role if has_permission(role, Permission.BREACH_DELETE)]
    assert holders == [Role.SYSTEM_ADMIN]


def test_pilgrims_cannot_reach_operator_surfaces() -> None:
    forbidden = {
        Permission.CROWD_VIEW_DETAIL,
        Permission.ALERT_ACKNOWLEDGE,
        Permission.INCIDENT_DISPATCH,
        Permission.PASS_SCAN,
        Permission.PASS_ADMIN,
        Permission.AUDIT_VIEW,
        Permission.USER_MANAGE,
        Permission.ANALYTICS_EXPORT,
    }
    assert forbidden.isdisjoint(permissions_for(Role.PILGRIM))


def test_pilgrims_can_do_the_four_things_they_need() -> None:
    for permission in (
        Permission.PASS_BOOK,
        Permission.SOS_RAISE,
        Permission.CROWD_VIEW_PUBLIC,
        Permission.INCIDENT_REPORT,
    ):
        assert has_permission(Role.PILGRIM, permission)


def test_volunteers_can_scan_but_not_acknowledge_alerts() -> None:
    # Section 2: volunteers close low-severity incidents; acknowledging an
    # alert is a Security Officer decision.
    assert has_permission(Role.VOLUNTEER, Permission.PASS_SCAN)
    assert has_permission(Role.VOLUNTEER, Permission.INCIDENT_UPDATE_LOW)
    assert not has_permission(Role.VOLUNTEER, Permission.ALERT_ACKNOWLEDGE)
    assert not has_permission(Role.VOLUNTEER, Permission.INCIDENT_UPDATE_ANY)


def test_responder_cannot_dispatch_to_itself() -> None:
    # Dispatch is a control-room decision; a responder updates and closes.
    assert has_permission(Role.RESPONDER, Permission.INCIDENT_UPDATE_ANY)
    assert not has_permission(Role.RESPONDER, Permission.INCIDENT_DISPATCH)


def test_mfa_is_required_for_the_two_privileged_roles() -> None:
    assert {Role.ADMINISTRATOR, Role.SYSTEM_ADMIN} == MFA_REQUIRED_ROLES
    assert requires_mfa(Role.ADMINISTRATOR)
    assert requires_mfa(Role.SYSTEM_ADMIN)
    assert not requires_mfa(Role.SECURITY_OFFICER)


def test_privileged_permissions_are_all_real_permissions() -> None:
    assert frozenset(Permission) >= PRIVILEGED_PERMISSIONS


def test_unknown_role_gets_nothing() -> None:
    assert permissions_for("temple-cat") == frozenset()
    assert not has_permission("temple-cat", Permission.PASS_BOOK)
    assert not requires_mfa("temple-cat")
