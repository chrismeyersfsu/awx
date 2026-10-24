"""
How AWX takes Jewel's pushes today (its /service-index/ endpoints) when AWX has drifted.

These tests record current behavior, they do not judge it.

Jewel pushes each change as the user who made it, with resource_api_actions "*", and only
logs a push AWX rejects. Writes through these endpoints are not pushed back to Jewel.
"""

import pytest

from ansible_base.rbac.models import RoleTeamAssignment, RoleUserAssignment

from awx.main.models import Credential, Inventory, Organization, Team, User

from .drift import corrupt_ansible_id
from .invariants import AwxOnlySnapshot, old_rbac_mismatches, sync_differences


def _writes_to_jewel(jewel):
    return [c for c in jewel.calls if c.method != "GET"]


def _pushed(jewel):
    return [(p.method, p.path.split("/")[0], p.status) for p in jewel.pushes]


@pytest.mark.django_db
def test_jewel_creates_org(world, jewel):
    """Baseline: Jewel creates org o2, AWX stores it under Jewel's ansible_id and service_id"""
    ansible_id = jewel.create("shared.organization", name="o2", description="")

    assert _pushed(jewel) == [("POST", "resources", 201)]
    resource = Organization.objects.get(name="o2").resource
    assert (str(resource.ansible_id), str(resource.service_id)) == (ansible_id, jewel.service_id)
    assert _writes_to_jewel(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "resource_type, natural_key, extra",
    [
        ("shared.organization", {"name": "o1"}, {"description": ""}),
        ("shared.team", {"name": "t1"}, {"description": ""}),
        ("shared.user", {"username": "bob"}, {}),
    ],
    ids=["org", "team", "user"],
)
def test_jewel_recreates_object_whose_delete_push_was_lost(world, jewel, pull, resource_type, natural_key, extra):
    """E1: Jewel deletes o1, t1 or bob, that push is lost, and Jewel creates one with the same name

    AWX refuses the create push with a 400 whose detail is the database's unique constraint error,
    and keeps its old copy under the old ansible_id. The next pull deletes that copy as an orphan,
    with the same cascades as P1 to P3, and creates Jewel's.
    """
    if resource_type == "shared.team":
        extra = dict(extra, organization=jewel.find("shared.organization", name="o1"))
    old = jewel.find(resource_type, **natural_key)
    with jewel.pushes_lost():
        jewel.delete(old)
    new = jewel.create(resource_type, **natural_key, **extra)
    push = jewel.pushes[-1]
    model = {"shared.organization": Organization, "shared.team": Team, "shared.user": User}[resource_type]
    obj = model.objects.get(**natural_key)

    assert push.status == 400, push.body
    assert list(push.body) == ["detail"]
    assert "UNIQUE" in push.body["detail"].upper()
    assert str(obj.resource.ansible_id) == old

    pull()

    assert not model.objects.filter(pk=obj.pk).exists()
    assert str(model.objects.get(**natural_key).resource.ansible_id) == new
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_jewel_assigns_role_to_user_awx_has(world, jewel):
    """Baseline: Jewel makes bob a member of t1, AWX grants it in old and new RBAC without pushing it back"""
    jewel.assign("Team Member", jewel.find("shared.user", username="bob"), jewel.find("shared.team", name="t1"))

    assert _pushed(jewel) == [("POST", "role-user-assignments", 201)]
    assert RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert world.team.member_role.members.filter(pk=world.bob.pk).exists()
    assert _writes_to_jewel(jewel) == []
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
@pytest.mark.parametrize("corrupted", ["bob", "team"])
def test_jewel_assigns_role_awx_cannot_resolve(world, jewel, pull, corrupted):
    """E2: Jewel makes bob a member of t1 while AWX has bob, or t1, under a bad ansible_id

    AWX refuses the push with a 400 naming the field and creates nothing. The next pull deletes and
    recreates the corrupted object (P2, P3) and grants the membership.
    """
    corrupt_ansible_id(getattr(world, corrupted))
    bob, t1 = jewel.find("shared.user", username="bob"), jewel.find("shared.team", name="t1")

    jewel.assign("Team Member", bob, t1)
    push = jewel.pushes[-1]

    assert push.status == 400
    if corrupted == "bob":
        assert push.body == {"user_ansible_id": [f"No user found with ansible_id={bob}"]}
    else:
        assert push.body == {"object_ansible_id": ["Resource with this ansible_id does not exist."]}
    assert not RoleUserAssignment.objects.filter(user__username="bob", role_definition__name="Team Member").exists()

    pull()

    assert RoleUserAssignment.objects.filter(user__username="bob", role_definition__name="Team Member", object_id=Team.objects.get(name="t1").pk).exists()
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_jewel_deletes_org(world, jewel):
    """E3: Jewel deletes o1, AWX deletes it with the same cascades as P1 and nothing comes back

    Its team t1 and credential cred1 are deleted, inventory inv1 loses its org, and the roles held
    through t1 go with it. Bob's role on inv1 stays.
    """
    snapshot = AwxOnlySnapshot.take()

    jewel.delete(jewel.find("shared.organization", name="o1"))

    assert _pushed(jewel) == [("DELETE", "resources", 204)]
    assert not Organization.objects.exists()
    assert not Team.objects.exists()
    assert not Credential.objects.exists()
    assert Inventory.objects.get(pk=world.inventory.pk).organization is None
    assert not RoleTeamAssignment.objects.exists()
    assert snapshot.losses() == [
        f"lost ('Credential', {world.credential.pk}, 'cred1', {world.org.pk})",
        f"lost ('Inventory', {world.inventory.pk}, 'inv1', {world.org.pk})",
        f"lost ('team', 't1', 'JobTemplate Execute', 'jobtemplate', '{world.jt.pk}')",
    ]
    assert _writes_to_jewel(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["team", "bob"])
def test_jewel_deletes_team_or_user(world, jewel, target):
    """E3: Jewel deletes t1 or bob, AWX deletes it and the roles it held on AWX-only objects"""
    obj = getattr(world, target)

    jewel.delete(str(obj.resource.ansible_id))

    assert _pushed(jewel) == [("DELETE", "resources", 204)]
    assert not type(obj).objects.filter(pk=obj.pk).exists()
    if target == "team":
        assert not RoleTeamAssignment.objects.filter(role_definition__name="JobTemplate Execute").exists()
        assert not RoleUserAssignment.objects.filter(role_definition__name="Team Member").exists()
    else:
        assert not RoleUserAssignment.objects.filter(role_definition__name="Inventory Use").exists()
    assert _writes_to_jewel(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize("other_team", [True, False], ids=["another_team_left", "last_team"])
def test_jewel_deletes_team_awx_has_under_another_ansible_id(world, jewel, pull, other_team):
    """E3: Jewel deletes t1 while AWX has t1 under a bad ansible_id

    The push gets a 404 and AWX keeps t1. If Jewel still has another team, the next pull deletes
    t1 as an orphan. If t1 was Jewel's last team, Jewel answers the team manifest with a 404,
    the pull skips teams (P6) and t1 stays in AWX, but the assignment sync takes its memberships.
    """
    if other_team:
        jewel.create("shared.team", name="t9", organization=jewel.find("shared.organization", name="o1"))
    corrupt_ansible_id(world.team)

    jewel.delete(jewel.find("shared.team", name="t1"))

    assert _pushed(jewel)[-1] == ("DELETE", "resources", 404)
    assert Team.objects.filter(pk=world.team.pk).exists()

    pull()

    assert Team.objects.filter(pk=world.team.pk).exists() is not other_team
    assert not RoleUserAssignment.objects.filter(role_definition__name="Team Member").exists()
    differences = sync_differences(jewel)
    if other_team:
        assert differences == []
    else:
        assert len(differences) == 1 and differences[0].startswith("shared.team t1") and differences[0].endswith("is only in AWX")
