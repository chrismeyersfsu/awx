"""
How AWX's push of a team save to Jewel (DAB's reverse sync) behaves today when Jewel fails or AWX's copy has drifted.

These tests record current behavior, they do not judge it.

DAB pushes a create, update or delete inside the save's transaction and turns any failure into a
ValidationError, so AWX answers 400 with DAB's generic message and rolls the change back. Updates
of resources AWX marks as its own are not pushed.
"""

import pytest
import requests

from ansible_base.rbac.models import RoleUserAssignment
from ansible_base.resource_registry.models import service_id

from awx.main.models import Team

from .drift import corrupt_ansible_id, give_ansible_id, make_local
from .invariants import old_rbac_mismatches, sync_differences

SYNC_FAILED = ["Failed to sync resource to resource server"]

FAILURES = {
    "jewel_400": dict(status=400, body={"name": ["Jewel rejects this."]}),
    "jewel_503": dict(status=503, body="Service Unavailable"),
    "unreachable": dict(exc=requests.ConnectionError("Jewel is down")),
}


def _admin(jewel):
    return jewel.jwt_headers(jewel.find("shared.user", username="admin"))


def _pushes(jewel):
    return [(c.method, c.path.rsplit("/service-index/", 1)[1], c.status) for c in jewel.calls if "/service-index/resources/" in c.path]


def _teams():
    return set(Team.objects.values_list("pk", "name", "description"))


def _create(api, world, headers):
    return api.post("/api/controller/v2/teams/", {"name": "t9", "organization": world.org.pk}, content_type="application/json", **headers)


def _update(api, world, headers):
    return api.patch(f"/api/controller/v2/teams/{world.team.pk}/", {"description": "changed in AWX"}, content_type="application/json", **headers)


def _delete(api, world, headers):
    return api.delete(f"/api/controller/v2/teams/{world.team.pk}/", **headers)


@pytest.mark.django_db
@pytest.mark.parametrize("failure", FAILURES.values(), ids=FAILURES.keys())
@pytest.mark.parametrize("action, method", [(_create, "POST"), (_update, "PUT"), (_delete, "DELETE")], ids=["create", "update", "delete"])
def test_jewel_failure_rejects_team_change(world, jewel, api, action, method, failure):
    """W1/W2: Jewel rejects the push, errors, or cannot be reached

    Every failure looks the same to the client: a 400 with DAB's generic message, without Jewel's
    reason or a hint that Jewel is down. The change is rolled back in AWX.
    """
    headers = _admin(jewel)
    jewel.fail(method, "*/service-index/resources/*", **failure)
    before = _teams()

    response = action(api, world, headers)

    assert response.status_code == 400, response.content
    assert response.json() == SYNC_FAILED
    [(pushed_method, _, status)] = _pushes(jewel)
    assert pushed_method == method
    assert status == failure.get("status")
    assert _teams() == before
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_update_team_with_corrupted_ansible_id(world, jewel, api):
    """W3: the update is pushed to an ansible_id Jewel does not know, Jewel answers 404 and AWX rejects the change

    The team cannot be changed in AWX until a pull deletes and recreates it (P2).
    """
    headers = _admin(jewel)
    bad_id = corrupt_ansible_id(world.team)

    response = _update(api, world, headers)

    assert response.status_code == 400, response.content
    assert response.json() == SYNC_FAILED
    assert _pushes(jewel) == [("PUT", f"resources/{bad_id}/", 404)]
    assert Team.objects.get(pk=world.team.pk).description == ""


@pytest.mark.django_db
def test_update_team_awx_marked_local(world, jewel, api, pull):
    """W4: AWX marks t1 as its own, so the update succeeds without a push and the next pull reverts it (P4)"""
    headers = _admin(jewel)
    make_local(world.team)

    response = _update(api, world, headers)

    assert response.status_code == 200, response.content
    assert _pushes(jewel) == []
    assert Team.objects.get(pk=world.team.pk).description == "changed in AWX"
    assert jewel.resources[jewel.find("shared.team", name="t1")].resource_data["description"] == ""

    pull()
    team = Team.objects.get(pk=world.team.pk)
    assert team.description == ""
    assert str(team.resource.service_id) == jewel.service_id


@pytest.mark.django_db
def test_delete_team_awx_marked_local(world, jewel, api):
    """W4: only updates skip the push for objects AWX marks as its own, a delete is pushed and removes the team in Jewel too"""
    headers = _admin(jewel)
    make_local(world.team)
    t1 = jewel.find("shared.team", name="t1")
    assert str(Team.objects.get(pk=world.team.pk).resource.service_id) == service_id()

    response = _delete(api, world, headers)

    assert response.status_code == 204, response.content
    assert _pushes(jewel) == [("DELETE", f"resources/{t1}/", 204)]
    assert not Team.objects.filter(pk=world.team.pk).exists()
    assert t1 not in jewel.resources


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["create", "rename"])
def test_team_name_jewel_already_has(world, jewel, api, action):
    """W5: Jewel created team t2 in o1 but the push never reached AWX, then AWX creates t2 or renames t1 to t2

    The fake answers the natural key clash with a 400 (not verified against Jewel), so AWX
    rejects the change and both sides keep one t2.
    """
    headers = _admin(jewel)
    with jewel.pushes_lost():
        t2 = jewel.create("shared.team", name="t2", organization=jewel.find("shared.organization", name="o1"), description="")
    before = _teams()

    if action == "create":
        response = api.post("/api/controller/v2/teams/", {"name": "t2", "organization": world.org.pk}, content_type="application/json", **headers)
    else:
        response = api.patch(f"/api/controller/v2/teams/{world.team.pk}/", {"name": "t2"}, content_type="application/json", **headers)

    assert response.status_code == 400, response.content
    assert response.json() == SYNC_FAILED
    assert _teams() == before
    assert jewel.find("shared.team", name="t2") == t2
    assert jewel.find("shared.team", name="t1")


@pytest.mark.django_db
def test_delete_team_with_corrupted_ansible_id(world, jewel, api):
    """W6: the delete is pushed to an ansible_id Jewel does not know, Jewel answers 404 and AWX keeps the team"""
    headers = _admin(jewel)
    bad_id = corrupt_ansible_id(world.team)

    response = _delete(api, world, headers)

    assert response.status_code == 400, response.content
    assert response.json() == SYNC_FAILED
    assert _pushes(jewel) == [("DELETE", f"resources/{bad_id}/", 404)]
    assert Team.objects.filter(pk=world.team.pk).exists()
    assert RoleUserAssignment.objects.filter(user=world.alice, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
def test_delete_team_holding_another_teams_ansible_id(world, jewel, api, pull):
    """W6: AWX's t1 carries the ansible_id of t2, a team whose create push never reached AWX, and AWX deletes t1

    AWX pushes the delete with t2's ansible_id, so Jewel deletes t2 and keeps t1. The next pull
    brings t1 back as a new team with its roles from Jewel.
    """
    headers = _admin(jewel)
    t1 = jewel.find("shared.team", name="t1")
    with jewel.pushes_lost():
        t2 = jewel.create("shared.team", name="t2", organization=jewel.find("shared.organization", name="o1"), description="")
    give_ansible_id(world.team, t2)

    response = _delete(api, world, headers)

    assert response.status_code == 204, response.content
    assert _pushes(jewel) == [("DELETE", f"resources/{t2}/", 204)]
    assert not Team.objects.filter(pk=world.team.pk).exists()
    assert t2 not in jewel.resources
    assert t1 in jewel.resources

    pull()
    team = Team.objects.get(name="t1")
    assert team.pk != world.team.pk
    assert str(team.resource.ansible_id) == t1
    assert RoleUserAssignment.objects.filter(user=world.alice, role_definition__name="Team Member", object_id=team.pk).exists()
    assert sync_differences(jewel) == []
