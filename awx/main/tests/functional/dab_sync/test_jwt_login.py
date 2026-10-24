"""
How AWX's JWT login (DAB's AwxJWTAuthentication) treats drifted users today.

These tests record current behavior, they do not judge it.
"""

import pytest

from ansible_base.rbac.models import RoleUserAssignment

from awx.main.models import User

from .drift import corrupt_ansible_id
from .invariants import AwxOnlySnapshot, old_rbac_mismatches


@pytest.mark.django_db
def test_login_in_sync(world, jewel, api):
    """Baseline for the fake: a clean login works and leaves roles alone"""
    response = api.get("/api/controller/v2/me/", **jewel.jwt_headers(jewel.find("shared.user", username="alice")))

    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    assert RoleUserAssignment.objects.filter(user=world.alice, role_definition__name="Team Member", object_id=world.team.pk).exists()


@pytest.mark.django_db
def test_login_repairs_user_with_corrupted_ansible_id(world, jewel, api):
    """J1: alice logs in through Jewel while AWX has her under a bad ansible_id

    Creating a user for the token's ansible_id clashes on username, so DAB takes the existing user
    by username and gives it the token's ansible_id. Alice keeps her user and roles.
    """
    corrupt_ansible_id(world.alice)
    alice_id = jewel.find("shared.user", username="alice")
    snapshot = AwxOnlySnapshot.take()

    response = api.get("/api/controller/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    alice = User.objects.get(username="alice")
    assert alice.pk == world.alice.pk
    assert str(alice.resource.ansible_id) == alice_id
    assert RoleUserAssignment.objects.filter(user=alice, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert snapshot.losses() == []
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
def test_reused_username_gets_a_new_user(world, jewel, api):
    """J4: bob is deleted in Jewel and a different person signs up as bob

    Jewel pushes the delete, which deletes AWX's bob with his roles, then pushes the new bob,
    which AWX creates as a new user. The new person has none of old bob's access.
    """
    new_bob = jewel.replace_user(jewel.find("shared.user", username="bob"))

    me = api.get("/api/controller/v2/me/", **jewel.jwt_headers(new_bob))
    inventory = api.get(f"/api/controller/v2/inventories/{world.inventory.pk}/", **jewel.jwt_headers(new_bob))

    assert [(p.method, p.status) for p in jewel.pushes] == [("DELETE", 204), ("POST", 201)]
    assert me.status_code == 200, me.content
    assert not User.objects.filter(pk=world.bob.pk).exists()
    assert me.json()["results"][0]["id"] != world.bob.pk
    assert inventory.status_code in (403, 404)
    assert not RoleUserAssignment.objects.filter(role_definition__name="Inventory Use", object_id=world.inventory.pk).exists()


@pytest.mark.django_db
def test_reused_username_takes_over_previous_user_when_pushes_are_lost(world, jewel, api):
    """J4: the same, but Jewel's pushes of the delete and of the new bob never reach AWX

    On login the new person's ansible_id is unknown, creating the user clashes on username, and
    DAB's fallback hands them old bob's AWX user, including the AWX-only Inventory Use role on inv1.
    """
    with jewel.pushes_lost():
        new_bob = jewel.replace_user(jewel.find("shared.user", username="bob"))

    me = api.get("/api/controller/v2/me/", **jewel.jwt_headers(new_bob))
    inventory = api.get(f"/api/controller/v2/inventories/{world.inventory.pk}/", **jewel.jwt_headers(new_bob))

    assert me.status_code == 200, me.content
    assert me.json()["results"][0]["id"] == world.bob.pk
    assert str(User.objects.get(pk=world.bob.pk).resource.ansible_id) == new_bob
    assert inventory.status_code == 200
    assert RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="Inventory Use", object_id=world.inventory.pk).exists()
