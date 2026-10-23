"""
How AWX's role assignment APIs behave today when the user's shared state has drifted from Jewel's.

These tests record current behavior, they do not judge it.
"""

import pytest

from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment

from .drift import corrupt_ansible_id
from .invariants import old_rbac_mismatches, sync_differences


def _new_api(api, world, headers):
    rd = RoleDefinition.objects.get(name="Team Member")
    return api.post(
        "/api/v2/role_user_assignments/",
        {"role_definition": rd.pk, "user": world.bob.pk, "object_id": world.team.pk},
        content_type="application/json",
        **headers,
    )


def _old_api(api, world, headers):
    return api.post(f"/api/v2/teams/{world.team.pk}/users/", {"id": world.bob.pk}, content_type="application/json", **headers)


@pytest.mark.django_db
@pytest.mark.parametrize("assign", [_new_api, _old_api], ids=["new_api", "old_api"])
def test_superuser_adds_corrupted_user_to_team(world, jewel, api, pull, assign):
    """R1/R2 (ticket case 2b): a superuser, logged in through Jewel, makes bob a member of t1 while AWX has bob under a bad ansible_id

    AWX pushes the assignment with bob's bad ansible_id, Jewel rejects it with a 400, and the
    HTTPError escapes DAB's reverse sync as a 500. The request is rolled back, so neither side has
    the assignment, and a pull leaves them in sync.
    """
    bad_id = corrupt_ansible_id(world.bob)
    headers = jewel.jwt_headers(jewel.find("shared.user", username="admin"))

    response = assign(api, world, headers)

    assert response.status_code == 500
    [push] = [c for c in jewel.calls if c.path.endswith("role-user-assignments/assign/")]
    assert push.data["user_ansible_id"] == bad_id
    assert push.status == 400
    assert not RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert old_rbac_mismatches() == []

    pull()
    assert sync_differences(jewel) == []
