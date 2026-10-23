"""
How AWX's periodic resource sync (the pull) treats each kind of drift today.

These tests record current behavior, they do not judge it. When a change in AWX, DAB or
Jewel makes one fail, the behavior changed and the test should be updated on purpose.
"""

import pytest

from ansible_base.rbac.models import RoleDefinition, RoleTeamAssignment, RoleUserAssignment
from ansible_base.resource_registry.models import service_id
from ansible_base.resource_registry.signals.handlers import no_reverse_sync

from awx.main.models import Credential, Inventory, Organization, Team, User

from .drift import corrupt_ansible_id, make_local
from .invariants import AwxOnlySnapshot, old_rbac_mismatches, sync_differences


def _has_user_role(user, role_name, obj):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name=role_name, object_id=obj.pk).exists()


@pytest.mark.django_db
def test_pull_in_sync_changes_nothing(world, jewel, pull):
    """Baseline for the fake: a pull right after adopting is a no-op"""
    before = (
        set(Organization.objects.values_list("pk", flat=True)),
        set(Team.objects.values_list("pk", flat=True)),
        set(User.objects.values_list("pk", flat=True)),
    )
    snapshot = AwxOnlySnapshot.take()

    pull()

    assert (
        set(Organization.objects.values_list("pk", flat=True)),
        set(Team.objects.values_list("pk", flat=True)),
        set(User.objects.values_list("pk", flat=True)),
    ) == before
    assert snapshot.losses() == []
    assert sync_differences(jewel) == []
    assert not [c for c in jewel.calls if c.method != "GET"], "a clean pull should only read from Jewel"


@pytest.mark.django_db
def test_pull_recreates_org_with_corrupted_ansible_id(world, jewel, pull):
    """P1: the org is not in Jewel's manifest under AWX's ansible_id, so orphan cleanup deletes it and the pull creates a new one

    Deleting the org cascades: its teams and credentials are deleted and its inventories lose their org.
    Teams come back from Jewel and their roles are re-granted, the credential does not come back.
    """
    corrupt_ansible_id(world.org)

    pull()

    assert not Organization.objects.filter(pk=world.org.pk).exists()
    org = Organization.objects.get(name="o1")
    assert str(org.resource.ansible_id) == jewel.find("shared.organization", name="o1")
    team = Team.objects.get(name="t1")
    assert team.pk != world.team.pk and team.organization == org
    assert not Credential.objects.filter(pk=world.credential.pk).exists()
    assert Inventory.objects.get(pk=world.inventory.pk).organization is None
    assert _has_user_role(world.alice, "Team Member", team)
    assert RoleTeamAssignment.objects.filter(team=team, role_definition__name="JobTemplate Execute", object_id=world.jt.pk).exists()
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_pull_recreates_team_with_corrupted_ansible_id(world, jewel, pull):
    """P2: same as the org, the team is deleted and created again, and its roles are re-granted from Jewel"""
    corrupt_ansible_id(world.team)

    pull()

    assert not Team.objects.filter(pk=world.team.pk).exists()
    team = Team.objects.get(name="t1")
    assert str(team.resource.ansible_id) == jewel.find("shared.team", name="t1")
    assert _has_user_role(world.alice, "Team Member", team)
    assert RoleTeamAssignment.objects.filter(team=team, role_definition__name="JobTemplate Execute", object_id=world.jt.pk).exists()
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("username, role_name, target", [("alice", "Team Member", "team"), ("bob", "Inventory Use", "inventory")])
def test_pull_recreates_user_with_corrupted_ansible_id(world, jewel, pull, username, role_name, target):
    """P3: same as the org, the user is deleted and created again, and their roles are re-granted from Jewel"""
    old = getattr(world, username)
    corrupt_ansible_id(old)

    pull()

    assert not User.objects.filter(pk=old.pk).exists()
    user = User.objects.get(username=username)
    assert str(user.resource.ansible_id) == jewel.find("shared.user", username=username)
    assert _has_user_role(user, role_name, getattr(world, target))
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("role_name, target", [("Team Admin", "team"), ("Organization Member", "org")])
def test_pull_removes_assignment_jewel_does_not_have(world, jewel, pull, role_name, target):
    """P9: a role on a shared object that only AWX has is removed by the pull, from both old and new RBAC"""
    obj = getattr(world, target)
    with no_reverse_sync():
        RoleDefinition.objects.get(name=role_name).give_permission(world.alice, obj)
    assert _has_user_role(world.alice, role_name, obj)
    assert not jewel.calls, "the assignment should exist only in AWX"
    snapshot = AwxOnlySnapshot.take()

    pull()

    assert not _has_user_role(world.alice, role_name, obj)
    assert snapshot.losses() == []
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["org", "team", "alice"])
def test_pull_leaves_unchanged_object_awx_marked_local(world, jewel, pull, target):
    """P4: AWX marks a shared object as its own, otherwise identical to Jewel's

    Its hash matches the manifest, so the pull skips it and AWX keeps owning it. Updates to it are
    then not reverse synced either, since DAB skips the push for locally owned resources.
    """
    obj = getattr(world, target)
    make_local(obj)

    pull()

    resource = type(obj).objects.get(pk=obj.pk).resource
    assert str(resource.service_id) == service_id()
    assert sync_differences(jewel) == [
        f"{jewel.resources[str(resource.ansible_id)].resource_type} {jewel.resources[str(resource.ansible_id)].natural_key()} "
        f"is owned by {service_id()} in AWX, by Jewel {jewel.service_id} there"
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["org", "team", "alice"])
def test_pull_takes_back_changed_object_awx_marked_local(world, jewel, pull, target):
    """P4: AWX marks a shared object as its own and changes it, the pull overwrites it with Jewel's copy and ownership"""
    obj = getattr(world, target)
    make_local(obj)
    model = type(obj)
    field = "first_name" if model is User else "description"
    model.objects.filter(pk=obj.pk).update(**{field: "changed in AWX"})

    pull()

    obj = model.objects.get(pk=obj.pk)
    assert getattr(obj, field) != "changed in AWX"
    assert str(obj.resource.service_id) == jewel.service_id
    assert sync_differences(jewel) == []
