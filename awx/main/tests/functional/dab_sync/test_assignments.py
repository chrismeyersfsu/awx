import pytest

from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment

from .drift import corrupt_ansible_id
from .invariants import old_rbac_mismatches, server_errors, sync_differences


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
    """R1/R2 (ticket case 2b): a superuser, logged in through Jewel, makes bob a member of t1 while AWX has bob under a bad ansible_id"""
    corrupt_ansible_id(world.bob)
    headers = jewel.jwt_headers(jewel.find("shared.user", username="admin"))

    response = assign(api, world, headers)

    problems = server_errors(response)
    accepted = response.status_code < 400
    has_role = RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="Team Member", object_id=world.team.pk).exists()
    if has_role != accepted:
        problems.append(f"responded {response.status_code} but AWX {'has' if has_role else 'does not have'} the assignment")
    problems += old_rbac_mismatches()

    pull()
    problems += [f"after pull: {p}" for p in sync_differences(jewel)]
    assert not problems, "\n".join(problems)
