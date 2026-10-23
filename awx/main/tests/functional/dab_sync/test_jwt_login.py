import pytest

from ansible_base.rbac.models import RoleUserAssignment

from awx.main.models import User

from .drift import corrupt_ansible_id
from .invariants import AwxOnlySnapshot, old_rbac_mismatches, server_errors


@pytest.mark.django_db
def test_login_repairs_user_with_corrupted_ansible_id(world, jewel, api):
    """J1: alice logs in through Jewel while AWX has her under a bad ansible_id, she keeps her user and roles"""
    corrupt_ansible_id(world.alice)
    alice_id = jewel.find("shared.user", username="alice")
    snapshot = AwxOnlySnapshot.take()

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert server_errors(response) == []
    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    alice = User.objects.get(username="alice")
    assert alice.pk == world.alice.pk
    assert str(alice.resource.ansible_id) == alice_id
    assert RoleUserAssignment.objects.filter(user=alice, role_definition__name="Team Member", object_id=world.team.pk).exists()
    assert snapshot.losses() == []
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
def test_login_in_sync(world, jewel, api):
    """Baseline for the fake: a clean login works and leaves roles alone"""
    response = api.get("/api/v2/me/", **jewel.jwt_headers(jewel.find("shared.user", username="alice")))

    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    assert RoleUserAssignment.objects.filter(user=world.alice, role_definition__name="Team Member", object_id=world.team.pk).exists()


@pytest.mark.django_db
def test_reused_username_does_not_inherit_previous_user(world, jewel, api):
    """J4: bob is deleted in Jewel and a different person signs up as bob, they must not get old bob's AWX roles and data"""
    new_bob = jewel.replace_user(jewel.find("shared.user", username="bob"))

    me = api.get("/api/v2/me/", **jewel.jwt_headers(new_bob))
    inventory = api.get(f"/api/v2/inventories/{world.inventory.pk}/", **jewel.jwt_headers(new_bob))

    problems = server_errors(me) + server_errors(inventory)
    if me.status_code == 200 and me.json()["results"][0]["id"] == world.bob.pk:
        problems.append(f"the new bob is logged in as the old bob's user, pk={world.bob.pk}")
    if inventory.status_code == 200:
        problems.append("the new bob can see inv1 through the old bob's Inventory Use role")
    assert not problems, "\n".join(problems)
