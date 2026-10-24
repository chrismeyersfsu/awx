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
    """W4: only updates skip the push for objects AWX marks as its own, a delete is pushed and removes the team in Jewel too

    AWX also asks Jewel to delete the role assignments on main.team, which Jewel only knows as
    shared.team, so that gets a 400 that AWX logs and ignores.
    """
    headers = _admin(jewel)
    make_local(world.team)
    t1 = jewel.find("shared.team", name="t1")
    assert str(Team.objects.get(pk=world.team.pk).resource.service_id) == service_id()

    response = _delete(api, world, headers)

    assert response.status_code == 204, response.content
    assert _pushes(jewel) == [("DELETE", f"resources/{t1}/", 204)]
    [object_delete] = [c for c in jewel.calls if c.path.endswith("/object-delete/")]
    assert (object_delete.data, object_delete.status) == ({"resource_type": "main.team", "resource_pk": str(world.team.pk)}, 400)
    assert not Team.objects.filter(pk=world.team.pk).exists()
    assert t1 not in jewel.resources


def _t2_create_push_lost(jewel):
    """Jewel creates team t2 in o1 and the push never reaches AWX, returning t2's ansible_id"""
    with jewel.pushes_lost():
        return jewel.create("shared.team", name="t2", organization=jewel.find("shared.organization", name="o1"), description="")


@pytest.mark.django_db
def test_create_team_jewel_already_has(world, jewel, api, pull):
    """W5: Jewel has t2, whose create push never reached AWX, and AWX creates t2

    Jewel's GetOrCreateProcessor takes the push as its existing t2 and gives it AWX's new
    ansible_id, so the create succeeds, both sides have one t2 under AWX's ansible_id and a pull
    changes nothing. Any other service still holding t2's old ansible_id is not told.
    """
    old = _t2_create_push_lost(jewel)
    alice = jewel.find("shared.user", username="alice")
    jewel.assign("Team Member", alice, old, by=jewel.find("shared.user", username="admin"))

    response = api.post("/api/controller/v2/teams/", {"name": "t2", "organization": world.org.pk}, content_type="application/json", **_admin(jewel))

    assert response.status_code == 201, response.content
    new = str(Team.objects.get(name="t2").resource.ansible_id)
    assert new != old
    assert old not in jewel.resources
    assert jewel.find("shared.team", name="t2") == new
    assert any(a.ansible_id_or_pk == new and a.role_definition_name == "Team Member" for a in jewel.assignments)

    pull()

    assert str(Team.objects.get(name="t2").resource.ansible_id) == new
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_rename_team_to_name_jewel_already_has(world, jewel, api, pull):
    """W5: Jewel has t2, whose create push never reached AWX, and AWX renames t1 to t2

    The update clashes with Jewel's t2 in the database, Jewel answers 400 and AWX rejects the
    rename. The next pull brings Jewel's t2 over.
    """
    t2 = _t2_create_push_lost(jewel)
    before = _teams()

    response = api.patch(f"/api/controller/v2/teams/{world.team.pk}/", {"name": "t2"}, content_type="application/json", **_admin(jewel))

    assert response.status_code == 400, response.content
    assert response.json() == SYNC_FAILED
    assert _pushes(jewel)[-1] == ("PUT", f"resources/{world.team.resource.ansible_id}/", 400)
    assert _teams() == before

    pull()

    assert str(Team.objects.get(name="t2").resource.ansible_id) == t2
    assert sync_differences(jewel) == []


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
