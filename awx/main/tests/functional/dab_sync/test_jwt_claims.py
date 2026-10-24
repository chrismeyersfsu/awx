"""
How AWX applies Jewel's changes to users and their roles today: by Jewel's push, and by the JWT login when a push was lost.

These tests record current behavior, they do not judge it.

Jewel pushes each change to AWX as it is made. A login maps the token's user_data onto the
user found by ansible_id, and when the token's claims hash differs from the hash of AWX's own
assignments, DAB fetches the claims from Jewel's jwt_claims endpoint and makes AWX's managed
role assignments match them.
"""

import pytest
import requests

from ansible_base.rbac.models import RoleTeamAssignment, RoleUserAssignment

from awx.main.models import Organization, Team, User

from .drift import corrupt_ansible_id
from .invariants import old_rbac_mismatches

DELIVERY = pytest.mark.parametrize("delivered", [True, False], ids=["pushed", "push_lost"])


def _alice(jewel):
    return jewel.find("shared.user", username="alice")


def _is_team_member(user, team):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name="Team Member", object_id=team.pk).exists()


def _claims_fetches(jewel):
    return [(c.method, c.status) for c in jewel.calls if "/jwt_claims/" in c.path]


def _me(api, jewel, ansible_id):
    return api.get("/api/controller/v2/me/", **jewel.jwt_headers(ansible_id))


@pytest.mark.django_db
@DELIVERY
def test_rename_in_jewel(world, jewel, api, delivered):
    """J3: Jewel renames alice to alicia. The push renames her AWX user in place, and if it was lost her next login does."""
    alice_id = _alice(jewel)
    with jewel.pushing(delivered):
        jewel.update(alice_id, username="alicia")
    after_push = User.objects.get(pk=world.alice.pk).username

    response = _me(api, jewel, alice_id)

    assert after_push == ("alicia" if delivered else "alice")
    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["username"] == "alicia"
    assert User.objects.get(pk=world.alice.pk).username == "alicia"
    assert not User.objects.filter(username="alice").exists()
    assert _is_team_member(world.alice, world.team)


@pytest.mark.django_db
def test_rename_in_jewel_to_username_awx_still_has(world, jewel, api):
    """J3: Jewel deletes bob, but that push is lost, then renames alice to bob

    AWX refuses the rename push (bob is still there), and alice's login skips the rename with a
    warning. Alice stays alice in AWX and the old bob is untouched.
    """
    alice_id = _alice(jewel)
    with jewel.pushes_lost():
        jewel.delete(jewel.find("shared.user", username="bob"))
    jewel.update(alice_id, username="bob")

    response = _me(api, jewel, alice_id)

    assert jewel.pushes[-1].status == 400
    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["id"] == world.alice.pk
    assert User.objects.get(pk=world.alice.pk).username == "alice"
    assert User.objects.get(pk=world.bob.pk).username == "bob"


@pytest.mark.django_db
@DELIVERY
def test_jewel_adds_user_to_new_org_and_team(world, jewel, api, delivered):
    """J5: Jewel creates org o2 with team t2 and makes alice a member

    Pushed, AWX has o2, t2 and the membership before alice logs in, so her claims match and AWX
    does not fetch them. With the pushes lost, her login fetches the claims and creates o2 and t2
    with Jewel's ansible_ids. Either way alice ends up a member in old and new RBAC.
    """
    alice_id = _alice(jewel)
    with jewel.pushing(delivered):
        o2 = jewel.create("shared.organization", name="o2", description="")
        t2 = jewel.create("shared.team", name="t2", organization=o2, description="")
        jewel.assign("Team Member", alice_id, t2)

    response = _me(api, jewel, alice_id)

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == ([] if delivered else [("GET", 200)])
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
    bad_id = corrupt_ansible_id(world.team)

    response = _me(api, jewel, _alice(jewel))

    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == []
    assert str(Team.objects.get(pk=world.team.pk).resource.ansible_id) == bad_id
    assert _is_team_member(world.alice, world.team)


@pytest.mark.django_db
def test_login_repairs_corrupted_team_for_new_member(world, jewel, api):
    """J6: AWX has t1 under a bad ansible_id and Jewel makes bob a member of t1, then bob logs in

    AWX refuses the push (it does not know t1's ansible_id). Bob's login fetches the claims, cannot
    find t1 by Jewel's ansible_id, and creating it clashes on (name, organization), so the natural
    key recovery from AAP-95186 gives AWX's t1 Jewel's ansible_id. t1 keeps its pk, members and
    roles, and bob becomes a member.
    """
    bob_id = jewel.find("shared.user", username="bob")
    t1 = jewel.find("shared.team", name="t1")
    corrupt_ansible_id(world.team)
    jewel.assign("Team Member", bob_id, t1)

    response = _me(api, jewel, bob_id)

    assert (jewel.pushes[-1].status, jewel.pushes[-1].body) == (400, {"object_ansible_id": ["Resource with this ansible_id does not exist."]})
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
@DELIVERY
def test_jewel_removes_membership(world, jewel, api, delivered):
    """J7: Jewel takes alice out of t1. The push removes the membership from old and new RBAC, and if it was lost her next login does."""
    alice_id = _alice(jewel)
    with jewel.pushing(delivered):
        jewel.unassign("Team Member", alice_id, jewel.find("shared.team", name="t1"))
    after_push = _is_team_member(world.alice, world.team)

    response = _me(api, jewel, alice_id)

    assert after_push is not delivered
    assert response.status_code == 200, response.content
    assert _claims_fetches(jewel) == ([] if delivered else [("GET", 200)])
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
    """J8: Jewel took alice out of t1 but the push was lost, and the jwt_claims fetch at her login fails

    DAB answers 401 and leaves her assignments as they were.
    """
    alice_id = _alice(jewel)
    with jewel.pushes_lost():
        jewel.unassign("Team Member", alice_id, jewel.find("shared.team", name="t1"))
    jewel.fail("GET", "*/jwt_claims/*", **failure)

    response = _me(api, jewel, alice_id)

    assert response.status_code == 401, response.content
    assert _claims_fetches(jewel) == [("GET", failure.get("status"))]
    assert _is_team_member(world.alice, world.team)
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
@pytest.mark.parametrize("username, status", [("alice", 401), ("admin", 200)])
def test_login_while_jewel_is_locked(world, jewel, api, username, status):
    """J8: Jewel made the user a Team Admin but the push was lost, and the jwt_claims fetch gets a 423 (Jewel is migrating)

    DAB lets superusers in and refuses everyone else, and applies no claims.
    """
    user_id = jewel.find("shared.user", username=username)
    with jewel.pushes_lost():
        jewel.assign("Team Admin", user_id, jewel.find("shared.team", name="t1"))
    jewel.fail("GET", "*/jwt_claims/*", status=423, body={"detail": "Locked"})

    response = _me(api, jewel, user_id)

    assert response.status_code == status, response.content
    assert _claims_fetches(jewel) == [("GET", 423)]
    assert not RoleUserAssignment.objects.filter(user__username=username, role_definition__name="Team Admin").exists()


@pytest.mark.django_db
@DELIVERY
def test_superuser_flag_from_jewel(world, jewel, api, delivered):
    """J9: Jewel makes alice a superuser and later takes it back

    The push sets is_superuser each time, and if it was lost her next login does. Her access to
    inv1 follows.
    """
    alice_id = _alice(jewel)
    inventory_url = f"/api/controller/v2/inventories/{world.inventory.pk}/"

    with jewel.pushing(delivered):
        jewel.update(alice_id, is_superuser=True)
    pushed_on = User.objects.get(pk=world.alice.pk).is_superuser
    me = _me(api, jewel, alice_id)
    as_superuser = api.get(inventory_url, **jewel.jwt_headers(alice_id))
    assert pushed_on is delivered
    assert me.status_code == 200, me.content
    assert me.json()["results"][0]["is_superuser"] is True
    assert as_superuser.status_code == 200

    with jewel.pushing(delivered):
        jewel.update(alice_id, is_superuser=False)
    pushed_off = User.objects.get(pk=world.alice.pk).is_superuser
    me = _me(api, jewel, alice_id)
    as_regular = api.get(inventory_url, **jewel.jwt_headers(alice_id))
    assert pushed_off is not delivered
    assert me.json()["results"][0]["is_superuser"] is False
    assert as_regular.status_code in (403, 404)
