"""
How AWX's periodic resource sync (the pull) behaves today when Jewel's answers are missing, incomplete or fail.

These tests record current behavior, they do not judge it.

The pull handles one resource type at a time (users, feature flags, role definitions, orgs,
teams): fetch the manifest, delete orphans unless the manifest looks incomplete, create or update
what differs, then sync role assignments for all types at the end. Assignment deletions are
skipped when Jewel's assignment list could not be fetched completely.
"""

import pytest
import requests

from ansible_base.rbac.models import RoleDefinition, RoleTeamAssignment, RoleUserAssignment
from ansible_base.rbac.role_sync_utils import AssignmentTuple
from ansible_base.resource_registry.signals.handlers import no_reverse_sync
from ansible_base.resource_registry.tasks.sync import ResourceSyncHTTPError

from awx.main.models import Team

from .drift import corrupt_ansible_id
from .invariants import AwxOnlySnapshot, old_rbac_mismatches, sync_differences

TEAM_MANIFEST = "*/resource-types/shared.team/manifest/"


def _is_team_member(user, team):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name="Team Member", object_id=team.pk).exists()


def _team_can_execute(team, jt):
    return RoleTeamAssignment.objects.filter(team=team, role_definition__name="JobTemplate Execute", object_id=jt.pk).exists()


def _manifest_csv(jewel, *ansible_ids):
    return "ansible_id,resource_hash\n" + "".join(f"{aid},{jewel.resources[aid].resource_hash()}\n" for aid in ansible_ids)


def _drop_alice_from_t1(jewel):
    jewel.assignments.discard(AssignmentTuple(jewel.find("shared.user", username="alice"), jewel.find("shared.team", name="t1"), "Team Member", "user"))


@pytest.mark.django_db
def test_pull_skips_type_without_manifest(world, jewel, pull):
    """P6: Jewel answers 404 for the team manifest, which is what DAB's manifest view does when it has none

    The pull skips teams: nothing is deleted and t1 keeps its members and roles.
    """
    jewel.fail("GET", TEAM_MANIFEST, status=404)

    pull()

    assert Team.objects.filter(pk=world.team.pk).exists()
    assert _is_team_member(world.alice, world.team)
    assert _team_can_execute(world.team, world.jt)
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_pull_deletes_every_team_for_empty_manifest(world, jewel, pull):
    """P6: Jewel answers 200 with an empty team manifest and X-Resource-Count 0

    DAB's own manifest view never does this (it answers 404 when empty), but if a Jewel or
    proxy does, the manifest looks complete and the pull deletes every team, with its roles.
    """
    jewel.fail("GET", TEAM_MANIFEST, status=200, body=_manifest_csv(jewel), headers={"X-Resource-Count": "0", "Content-Type": "text/csv"})

    pull()

    assert not Team.objects.exists()
    assert not RoleUserAssignment.objects.filter(role_definition__name="Team Member").exists()
    assert not RoleTeamAssignment.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "manifest",
    [
        dict(status=404),
        dict(headers={"Content-Type": "text/csv"}),
        dict(headers={"X-Resource-Count": "2", "Content-Type": "text/csv"}),
    ],
    ids=["not_found", "no_count_header", "count_mismatch"],
)
def test_pull_with_unusable_manifest_and_corrupted_team(world, jewel, pull, manifest):
    """P6: AWX has t1 under a bad ansible_id and the team manifest is missing or looks incomplete

    Orphan cleanup is skipped, but that does not keep t1 as it was:
    - not_found and no_count_header: no team is processed, so t1 survives with the bad ansible_id.
      The assignment sync still runs, AWX's assignments on t1 name the bad ansible_id, which
      Jewel does not have, so they are deleted, and Jewel's name t1's real ansible_id, which
      AWX does not have, so they are not created. t1 is left without members or roles.
    - count_mismatch: Jewel's t1 is processed. Creating it clashes on the name, the pull's
      conflict handling finds AWX's t1 is unknown to Jewel and deletes it, then creates t1 again
      with its roles, the same end state as P2.
    """
    t1 = jewel.find("shared.team", name="t1")
    bad_id = corrupt_ansible_id(world.team)
    if manifest.get("status") != 404:
        manifest = dict(manifest, status=200, body=_manifest_csv(jewel, t1))
    jewel.fail("GET", TEAM_MANIFEST, **manifest)

    pull()

    team = Team.objects.get(name="t1")
    if "X-Resource-Count" in manifest.get("headers", {}):
        assert team.pk != world.team.pk
        assert str(team.resource.ansible_id) == t1
        assert _is_team_member(world.alice, team)
        assert _team_can_execute(team, world.jt)
    else:
        assert team.pk == world.team.pk
        assert str(team.resource.ansible_id) == bad_id
        assert not _is_team_member(world.alice, team)
        assert not _team_can_execute(team, world.jt)
    assert old_rbac_mismatches() == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path_glob, failure, raised",
    [
        ("*/metadata/", dict(status=500), requests.HTTPError),
        ("*/metadata/", dict(exc=requests.ConnectionError("Jewel is down")), requests.ConnectionError),
        (TEAM_MANIFEST, dict(status=500), ResourceSyncHTTPError),
    ],
    ids=["metadata_500", "metadata_unreachable", "team_manifest_500"],
)
def test_pull_stops_when_jewel_fails(world, jewel, pull, path_glob, failure, raised):
    """P7: Jewel dropped alice from t1, and a request the pull needs for every type fails

    The pull raises on the first failure and never reaches the assignment sync, so alice stays a
    member until a pull gets through. Types handled before the failure are already synced (teams
    come last, after users and orgs).
    """
    _drop_alice_from_t1(jewel)
    jewel.fail("GET", path_glob, **failure)

    with pytest.raises(raised):
        pull()

    assert _is_team_member(world.alice, world.team)
    assert not [c for c in jewel.calls if "assignments" in c.path]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "failure",
    [dict(status=500), dict(status=404), dict(exc=requests.ConnectionError("Jewel is down"))],
    ids=["jewel_500", "jewel_404", "unreachable"],
)
def test_pull_fails_fetching_team_after_deleting_it(world, jewel, pull, failure):
    """P7: AWX has t1 under a bad ansible_id and fetching Jewel's t1 fails

    Orphan cleanup has already deleted AWX's t1, so AWX has no t1 until a later pull gets through,
    which recreates it with its roles. A 500 or a dropped connection makes the pull raise, a 404
    marks t1 unavailable and the pull carries on.
    """
    t1 = jewel.find("shared.team", name="t1")
    corrupt_ansible_id(world.team)
    jewel.fail("GET", f"*/service-index/resources/{t1}/", **failure)
    snapshot = AwxOnlySnapshot.take()

    if failure.get("status") == 404:
        pull()
    else:
        with pytest.raises((requests.HTTPError, requests.ConnectionError)):
            pull()

    assert not Team.objects.filter(name="t1").exists()
    assert not RoleUserAssignment.objects.filter(role_definition__name="Team Member").exists()

    jewel.failures.clear()
    pull()
    team = Team.objects.get(name="t1")
    assert _is_team_member(world.alice, team)
    assert _team_can_execute(team, world.jt)
    assert snapshot.losses() == []
    assert old_rbac_mismatches() == []
    assert sync_differences(jewel) == []


@pytest.mark.django_db
def test_pull_removes_job_template_assignment_jewel_does_not_have(world, jewel, pull):
    """P8: bob has JobTemplate Admin on jt1 only in AWX, the pull removes it

    AWX's pull compares all of AWX's assignments with Jewel's, not only those on shared objects,
    so a role on an AWX-only object that never reached Jewel is removed like P9.
    """
    with no_reverse_sync():
        RoleDefinition.objects.get(name="JobTemplate Admin").give_permission(world.bob, world.jt)

    pull()

    assert not RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="JobTemplate Admin").exists()
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path_glob, failure",
    [
        ("*/role-user-assignments/", dict(status=500)),
        ("*/role-user-assignments/", dict(exc=requests.ConnectionError("Jewel is down"))),
        ("*/role-team-assignments/", dict(status=500)),
    ],
    ids=["user_list_500", "user_list_unreachable", "team_list_500"],
)
def test_pull_keeps_assignments_when_assignment_list_fails(world, jewel, pull, path_glob, failure):
    """P8: bob has JobTemplate Admin on jt1 only in AWX and Jewel's assignment list fails

    The fetch is incomplete, so the pull deletes no assignments and finishes without raising.
    """
    with no_reverse_sync():
        RoleDefinition.objects.get(name="JobTemplate Admin").give_permission(world.bob, world.jt)
    jewel.fail("GET", path_glob, **failure)

    pull()

    assert RoleUserAssignment.objects.filter(user=world.bob, role_definition__name="JobTemplate Admin").exists()
    assert _is_team_member(world.alice, world.team)
