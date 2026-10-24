"""
How AWX follows today when Jewel renames or deletes a role definition, and what happens to the Compat role definitions AWX makes.

These tests record current behavior, they do not judge it.

The pull syncs role definitions like other shared resources. AWX's bridge to its old RBAC
(ROLE_DEFINITION_TO_ROLE_FIELD) and the JWT managed roles (ANSIBLE_BASE_JWT_MANAGED_ROLES) both go
by role definition name.
"""

import pytest

from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment
from ansible_base.rbac.role_sync_utils import AssignmentTuple

from .fake_jewel import ROLE_DEFINITION
from .invariants import old_rbac_mismatches, sync_differences


def _has_role(user, role_name, obj):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name=role_name, object_id=obj.pk).exists()


def _rename_team_member(jewel):
    jewel.rename_role_definition("Team Member", "Team Participant")
    return jewel.find("shared.team", name="t1")


@pytest.mark.django_db
def test_pull_renames_role_definition(world, jewel, pull):
    """P10: Jewel renames Team Member to Team Participant and adds bob to t1 under the new name

    The pull renames AWX's role definition, so alice keeps her assignment and her old member_role
    membership. Bob gets the assignment but not the old member_role membership: the bridge to old
    RBAC knows the role only as Team Member, so /api/v2/teams/N/users/ does not list him.
    """
    t1 = _rename_team_member(jewel)
    jewel.assignments.add(AssignmentTuple(jewel.find("shared.user", username="bob"), t1, "Team Participant", "user"))

    pull()

    assert not RoleDefinition.objects.filter(name="Team Member").exists()
    assert _has_role(world.alice, "Team Participant", world.team)
    assert _has_role(world.bob, "Team Participant", world.team)
    assert set(world.team.member_role.members.values_list("username", flat=True)) == {"alice"}
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_login_ignores_renamed_role_definition(world, jewel, pull, api):
    """P10: after the rename, Jewel drops alice from t1 and she logs in

    Team Participant is not a JWT managed role, so neither her claims nor AWX's include it. The
    hashes match, AWX does not fetch claims, and alice stays a member until the next pull.
    """
    t1 = _rename_team_member(jewel)
    pull()
    alice = jewel.find("shared.user", username="alice")
    jewel.assignments.discard(AssignmentTuple(alice, t1, "Team Participant", "user"))

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice))

    assert response.status_code == 200, response.content
    assert not [c for c in jewel.calls if "/jwt_claims/" in c.path]
    assert _has_role(world.alice, "Team Participant", world.team)

    pull()
    assert not _has_role(world.alice, "Team Participant", world.team)


@pytest.mark.django_db
def test_pull_deletes_role_definition_jewel_deleted(world, jewel, pull):
    """P10: bob is a Team Admin of t1 and Jewel deletes the Team Admin role definition

    The pull deletes AWX's role definition with its assignments, and bob leaves the old admin_role too.
    """
    RoleDefinition.objects.get(name="Team Admin").give_permission(world.bob, world.team)
    assert world.team.admin_role.members.filter(pk=world.bob.pk).exists()
    jewel.delete_role_definition("Team Admin")

    pull()

    assert not RoleDefinition.objects.filter(name="Team Admin").exists()
    assert not RoleUserAssignment.objects.filter(user=world.bob, content_type__model="team", object_id=world.team.pk).exists()
    assert not world.team.admin_role.members.filter(pk=world.bob.pk).exists()
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("jewel_deletes", [False, True], ids=["kept_in_jewel", "deleted_in_jewel"])
def test_compat_role_definition_through_pull(world, jewel, pull, jewel_deletes):
    """P10: adding bob to jt1's old read_role makes AWX create JobTemplate Read Compat, which has no managed equivalent

    AWX pushes the Compat role definition and the assignment to Jewel, which then owns the role
    definition. A pull leaves it alone while Jewel has it. When Jewel deletes it, the pull
    deletes it in AWX with bob's assignment, but bob stays a member of the old read_role.
    """
    world.jt.read_role.members.add(world.bob)
    compat = jewel.find(ROLE_DEFINITION, name="JobTemplate Read Compat")
    assert str(RoleDefinition.objects.get(name="JobTemplate Read Compat").resource.service_id) == jewel.service_id
    assert AssignmentTuple(jewel.find("shared.user", username="bob"), str(world.jt.pk), "JobTemplate Read Compat", "user") in jewel.assignments
    if jewel_deletes:
        jewel.delete_role_definition("JobTemplate Read Compat")

    pull()

    assert RoleDefinition.objects.filter(name="JobTemplate Read Compat").exists() is not jewel_deletes
    assert _has_role(world.bob, "JobTemplate Read Compat", world.jt) is not jewel_deletes
    assert world.jt.read_role.members.filter(pk=world.bob.pk).exists()
    assert (compat in jewel.resources) is not jewel_deletes
    assert sync_differences(jewel) == []
