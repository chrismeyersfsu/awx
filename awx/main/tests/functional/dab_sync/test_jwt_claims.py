"""
How AWX's JWT login applies Jewel's user data and claims today, when they differ from what AWX has.

These tests record current behavior, they do not judge it.

A login maps the token's user_data onto the user found by ansible_id. When the token's claims
hash differs from the hash of AWX's own assignments, DAB fetches the claims from Jewel's
jwt_claims endpoint and makes AWX's managed role assignments match them.
"""

import pytest
import requests

from ansible_base.rbac.models import RoleTeamAssignment, RoleUserAssignment
from ansible_base.rbac.role_sync_utils import AssignmentTuple

from awx.main.models import Organization, Team, User

from .drift import corrupt_ansible_id
from .invariants import old_rbac_mismatches


def _alice(jewel):
    return jewel.find("shared.user", username="alice")


def _is_team_member(user, team):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name="Team Member", object_id=team.pk).exists()


def _claims_fetches(jewel):
    return [(c.method, c.status) for c in jewel.calls if "/jwt_claims/" in c.path]


@pytest.mark.django_db
def test_login_renames_user_renamed_in_jewel(world, jewel, api):
    """J3: alice is renamed to alicia in Jewel, her next login renames her AWX user in place"""
    alice_id = _alice(jewel)
    jewel.resources[alice_id].resource_data["username"] = "alicia"

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["username"] == "alicia"
    assert User.objects.get(pk=world.alice.pk).username == "alicia"
    assert not User.objects.filter(username="alice").exists()
    assert _is_team_member(world.alice, world.team)


@pytest.mark.django_db
def test_login_keeps_name_when_jewel_rename_clashes(world, jewel, api):
    """J3: Jewel renames alice to bob while AWX still has a bob, DAB skips the rename and logs a warning

    Alice logs in as herself, still named alice, and bob is untouched.
    """
    alice_id = _alice(jewel)
    jewel.resources[alice_id].resource_data["username"] = "bob"

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    assert User.objects.get(pk=world.alice.pk).username == "alice"
    assert User.objects.get(pk=world.bob.pk).username == "bob"


@pytest.mark.django_db
def test_login_creates_org_and_team_awx_does_not_have(world, jewel, api):
    """J5: Jewel made alice a member of team t2 in org o2, which AWX does not have yet

    The claims hash differs, so AWX fetches the claims and creates o2 and t2 with Jewel's
    ansible_ids, then makes alice a member in both old and new RBAC.
    """
    alice_id = _alice(jewel)
    o1 = jewel.find("shared.organization", name="o1")
    o2 = jewel.add("shared.organization", **dict(jewel.resources[o1].resource_data, name="o2"))
    t1 = jewel.find("shared.team", name="t1")
    t2 = jewel.add("shared.team", **dict(jewel.resources[t1].resource_data, name="t2", organization=o2))
    jewel.assignments.add(AssignmentTuple(alice_id, t2, "Team Member", "user"))

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == [("GET", 200)]
    org = Organization.objects.get(name="o2")
    team = Team.objects.get(name="t2")
    assert str(org.resource.ansible_id) == o2
    assert str(team.resource.ansible_id) == t2
    assert team.organization == org
    assert _is_team_member(world.alice, team)
    assert team.member_role.members.filter(pk=world.alice.pk).exists()
    assert _is_team_member(world.alice, world.team)
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
def test_login_leaves_corrupted_team_for_existing_member(world, jewel, api):
    """J6: AWX has t1 under a bad ansible_id and alice, already a member, logs in

    Assignments keep their own copy of the object's ansible_id (object_ansible_id), which still is
    Jewel's, so AWX's claims hash matches the token's. AWX does not fetch the claims and t1 keeps
    the bad ansible_id.
    """
    alice_id = _alice(jewel)
    bad_id = corrupt_ansible_id(world.team)

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == []
    assert str(Team.objects.get(pk=world.team.pk).resource.ansible_id) == bad_id
    assert _is_team_member(world.alice, world.team)


@pytest.mark.django_db
def test_login_repairs_corrupted_team_for_new_member(world, jewel, api):
    """J6: AWX has t1 under a bad ansible_id and Jewel made bob a member of t1, bob logs in

    AWX fetches the claims and cannot find t1 by Jewel's ansible_id. Creating it clashes on
    (name, organization), and the natural key recovery from AAP-95186 gives AWX's t1 Jewel's
    ansible_id. t1 keeps its pk, its members and its roles, and bob becomes a member.
    """
    bob_id = jewel.find("shared.user", username="bob")
    t1 = jewel.find("shared.team", name="t1")
    corrupt_ansible_id(world.team)
    jewel.assignments.add(AssignmentTuple(bob_id, t1, "Team Member", "user"))

    response = api.get("/api/v2/me/", **jewel.jwt_headers(bob_id))

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == [("GET", 200)]
    team = Team.objects.get(name="t1")
    assert team.pk == world.team.pk
    assert str(team.resource.ansible_id) == t1
    assert _is_team_member(world.bob, team)
    assert _is_team_member(world.alice, team)
    assert RoleTeamAssignment.objects.filter(team=team, role_definition__name="JobTemplate Execute", object_id=world.jt.pk).exists()
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
def test_login_removes_membership_jewel_dropped(world, jewel, api):
    """J7: Jewel no longer has alice in t1, her login removes the membership from old and new RBAC

    The removal is local, AWX does not push an unassign back to Jewel.
    """
    alice_id = _alice(jewel)
    t1 = jewel.find("shared.team", name="t1")
    jewel.assignments.discard(AssignmentTuple(alice_id, t1, "Team Member", "user"))

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == [("GET", 200)]
    assert not _is_team_member(world.alice, world.team)
    assert not world.team.member_role.members.filter(pk=world.alice.pk).exists()
    assert not [c for c in jewel.calls if c.method != "GET"]
    assert old_rbac_mismatches() == []


FETCH_FAILURES = {
    "jewel_404": dict(status=404, body={"error": "User not found"}),
    "jewel_500": dict(status=500, body="Internal Server Error"),
    "unreachable": dict(exc=requests.ConnectionError("Jewel is down")),
}


@pytest.mark.django_db
@pytest.mark.parametrize("failure", FETCH_FAILURES.values(), ids=FETCH_FAILURES.keys())
def test_login_fails_when_claims_fetch_fails(world, jewel, api, failure):
    """J8: alice's claims changed in Jewel and the jwt_claims fetch fails

    DAB answers 401 and leaves her assignments as they were.
    """
    alice_id = _alice(jewel)
    jewel.assignments.discard(AssignmentTuple(alice_id, jewel.find("shared.team", name="t1"), "Team Member", "user"))
    jewel.fail("GET", "*/jwt_claims/*", **failure)

    response = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))

    assert response.status_code == 401, response.content
    assert _claims_fetches(jewel) == [("GET", failure.get("status"))]
    assert _is_team_member(world.alice, world.team)
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
@pytest.mark.parametrize("username, status", [("alice", 401), ("admin", 200)])
def test_login_while_jewel_is_locked(world, jewel, api, username, status):
    """J8: the jwt_claims fetch gets a 423 (Jewel is migrating), DAB lets superusers in and refuses everyone else"""
    user_id = jewel.find("shared.user", username=username)
    jewel.assignments.add(AssignmentTuple(user_id, jewel.find("shared.team", name="t1"), "Team Admin", "user"))
    jewel.fail("GET", "*/jwt_claims/*", status=423, body={"detail": "Locked"})

    response = api.get("/api/v2/me/", **jewel.jwt_headers(user_id))

    assert response.status_code == status, response.content
    assert _claims_fetches(jewel) == [("GET", 423)]
    assert not RoleUserAssignment.objects.filter(user__username=username, role_definition__name="Team Admin").exists()


@pytest.mark.django_db
def test_login_follows_superuser_flag(world, jewel, api):
    """J9: Jewel makes alice a superuser and later takes it back, each login sets is_superuser to match"""
    alice_id = _alice(jewel)
    inventory_url = f"/api/v2/inventories/{world.inventory.pk}/"

    jewel.resources[alice_id].resource_data["is_superuser"] = True
    me = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))
    as_superuser = api.get(inventory_url, **jewel.jwt_headers(alice_id))
    assert me.status_code == 200, me.content
    assert me.json()["results"][0]["is_superuser"] is True
    assert User.objects.get(pk=world.alice.pk).is_superuser
    assert as_superuser.status_code == 200

    jewel.resources[alice_id].resource_data["is_superuser"] = False
    me = api.get("/api/v2/me/", **jewel.jwt_headers(alice_id))
    as_regular = api.get(inventory_url, **jewel.jwt_headers(alice_id))
    assert me.json()["results"][0]["is_superuser"] is False
    assert not User.objects.get(pk=world.alice.pk).is_superuser
    assert as_regular.status_code in (403, 404)
