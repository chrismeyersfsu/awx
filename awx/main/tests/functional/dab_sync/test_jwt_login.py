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
