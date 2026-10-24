"""
How AWX's service-index endpoints, which Jewel calls to push changes to AWX, behave today when AWX has drifted.

These tests record current behavior, they do not judge it.

Jewel calls them with a JWT, here the superuser's. Writes through them are not pushed back to Jewel.
"""

import uuid

import pytest

from ansible_base.rbac.models import RoleTeamAssignment, RoleUserAssignment
from ansible_base.resource_registry.models import Resource

from awx.main.models import Credential, Inventory, Organization, Team, User

from .invariants import AwxOnlySnapshot, old_rbac_mismatches

RESOURCES = "/api/v2/service-index/resources/"
ASSIGN = "/api/v2/service-index/role-user-assignments/assign/"


def _admin(jewel):
    return jewel.jwt_headers(jewel.find("shared.user", username="admin"))


def _writes_to_jewel(jewel):
    return [c for c in jewel.calls if c.method != "GET"]


def _shared_state():
    return (
        set(
            Resource.objects.filter(content_type__resource_type__name__in=("shared.organization", "shared.team", "shared.user")).values_list(
                "ansible_id", "service_id"
            )
        ),
        set(Organization.objects.values_list("pk", "name")),
        set(Team.objects.values_list("pk", "name")),
        set(User.objects.values_list("pk", "username")),
    )


@pytest.mark.django_db
def test_jewel_creates_org(world, jewel, api):
    """Baseline: Jewel creates org o2, AWX stores it under Jewel's ansible_id and service_id"""
    ansible_id = str(uuid.uuid4())
    body = {
        "resource_type": "shared.organization",
        "ansible_id": ansible_id,
        "service_id": jewel.service_id,
        "resource_data": {"name": "o2", "description": ""},
    }

    response = api.post(RESOURCES, body, content_type="application/json", **_admin(jewel))

    assert response.status_code == 201, response.content
    resource = Organization.objects.get(name="o2").resource
    assert (str(resource.ansible_id), str(resource.service_id)) == (ansible_id, jewel.service_id)
    assert _writes_to_jewel(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "resource_type, resource_data",
    [
        ("shared.organization", {"name": "o1", "description": ""}),
        ("shared.team", {"name": "t1", "description": ""}),
        ("shared.user", {"username": "bob"}),
    ],
    ids=["org", "team", "user"],
)
def test_jewel_creates_name_awx_has_under_another_ansible_id(world, jewel, api, resource_type, resource_data):
    """E1: Jewel creates o1, t1 or bob under a new ansible_id while AWX has it under the old one

    AWX answers 400 with the database's unique constraint error as the detail, and changes nothing.
    The existing object is not adopted under the new ansible_id, so the next pull treats it as an
    orphan (P1 to P3).
    """
    if resource_type == "shared.team":
        resource_data = dict(resource_data, organization=jewel.find("shared.organization", name="o1"))
    body = {"resource_type": resource_type, "ansible_id": str(uuid.uuid4()), "service_id": jewel.service_id, "resource_data": resource_data}
    before = _shared_state()

    response = api.post(RESOURCES, body, content_type="application/json", **_admin(jewel))

    assert response.status_code == 400, response.content
    assert list(response.json()) == ["detail"]
    assert "UNIQUE" in response.json()["detail"].upper()
    assert _shared_state() == before


@pytest.mark.django_db
def test_jewel_creates_ansible_id_awx_already_has(world, jewel, api):
    """E1: Jewel creates o1 again under the ansible_id AWX already has for it, AWX answers 400 and changes nothing"""
    body = {
        "resource_type": "shared.organization",
        "ansible_id": jewel.find("shared.organization", name="o1"),
        "service_id": jewel.service_id,
        "resource_data": {"name": "o1", "description": "again"},
    }
    before = _shared_state()

    response = api.post(RESOURCES, body, content_type="application/json", **_admin(jewel))

    assert response.status_code == 400, response.content
    assert response.json() == {"ansible_id": ["resource with this ansible id already exists."]}
    assert _shared_state() == before
    assert Organization.objects.get(pk=world.org.pk).description == ""


@pytest.mark.django_db
def test_jewel_assigns_role_to_user_awx_has(world, jewel, api):
    """Baseline: Jewel makes bob a member of t1, AWX grants it in old and new RBAC without pushing it back"""
    body = {
        "user_ansible_id": jewel.find("shared.user", username="bob"),
        "role_definition": "Team Member",
        "object_ansible_id": jewel.find("shared.team", name="t1"),
    }

    response = api.post(ASSIGN, body, content_type="application/json", **_admin(jewel))

    assert response.status_code == 201, response.content
    assert RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert world.team.member_role.members.filter(pk=world.bob.pk).exists()
    assert _writes_to_jewel(jewel) == []
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
@pytest.mark.parametrize("missing", ["user", "team"])
def test_jewel_assigns_role_awx_cannot_resolve(world, jewel, api, missing):
    """E2: Jewel makes a user a member of a team and AWX does not know the user's or the team's ansible_id

    This is what AWX sees for a user or team Jewel created that AWX has not pulled yet, or one
    AWX has under a corrupted ansible_id. AWX answers 400 naming the field and creates nothing.
    """
    unknown = str(uuid.uuid4())
    body = {
        "user_ansible_id": unknown if missing == "user" else jewel.find("shared.user", username="bob"),
        "role_definition": "Team Member",
        "object_ansible_id": unknown if missing == "team" else jewel.find("shared.team", name="t1"),
    }
    before = set(RoleUserAssignment.objects.values_list("pk", flat=True))

    response = api.post(ASSIGN, body, content_type="application/json", **_admin(jewel))

    assert response.status_code == 400, response.content
    if missing == "user":
        assert response.json() == {"user_ansible_id": [f"No user found with ansible_id={unknown}"]}
    else:
        assert response.json() == {"object_ansible_id": ["Resource with this ansible_id does not exist."]}
    assert set(RoleUserAssignment.objects.values_list("pk", flat=True)) == before


@pytest.mark.django_db
def test_jewel_deletes_org(world, jewel, api):
    """E3: Jewel deletes o1, AWX deletes it with the same cascades as P1 and nothing comes back

    Its team t1 and credential cred1 are deleted, inventory inv1 loses its org, and the roles held
    through t1 go with it. Bob's role on inv1 stays.
    """
    snapshot = AwxOnlySnapshot.take()

    response = api.delete(f"{RESOURCES}{jewel.find('shared.organization', name='o1')}/", **_admin(jewel))

    assert response.status_code == 204, response.content
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
def test_jewel_deletes_team_or_user(world, jewel, api, target):
    """E3: Jewel deletes t1 or bob, AWX deletes it and the roles it held on AWX-only objects"""
    obj = getattr(world, target)
    ansible_id = str(obj.resource.ansible_id)

    response = api.delete(f"{RESOURCES}{ansible_id}/", **_admin(jewel))

    assert response.status_code == 204, response.content
    assert not type(obj).objects.filter(pk=obj.pk).exists()
    if target == "team":
        assert not RoleTeamAssignment.objects.filter(role_definition__name="JobTemplate Execute").exists()
        assert not RoleUserAssignment.objects.filter(role_definition__name="Team Member").exists()
    else:
        assert not RoleUserAssignment.objects.filter(role_definition__name="Inventory Use").exists()
    assert _writes_to_jewel(jewel) == []


@pytest.mark.django_db
def test_jewel_deletes_ansible_id_awx_does_not_have(world, jewel, api):
    """E3: Jewel deletes an ansible_id AWX does not know, such as t1's real one while AWX has t1 under a bad one: 404 and nothing changes"""
    before = _shared_state()

    response = api.delete(f"{RESOURCES}{uuid.uuid4()}/", **_admin(jewel))

    assert response.status_code == 404, response.content
    assert _shared_state() == before
