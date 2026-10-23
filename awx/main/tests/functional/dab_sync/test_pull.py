import pytest

from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment
from ansible_base.resource_registry.signals.handlers import no_reverse_sync

from awx.main.models import Organization, Team, User

from .drift import corrupt_ansible_id, make_local
from .invariants import AwxOnlySnapshot, old_rbac_mismatches, sync_differences


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target, resource_type, natural_key",
    [
        ("org", "shared.organization", {"name": "o1"}),
        ("team", "shared.team", {"name": "t1"}),
        ("alice", "shared.user", {"username": "alice"}),
        ("bob", "shared.user", {"username": "bob"}),
    ],
)
def test_pull_repairs_corrupted_ansible_id_in_place(world, jewel, pull, target, resource_type, natural_key):
    """P1-P3: AWX has the object under an ansible_id Jewel never issued, a pull fixes the id without recreating the object"""
    obj = getattr(world, target)
    snapshot = AwxOnlySnapshot.take()
    corrupt_ansible_id(obj)

    pull()

    model = type(obj)
    problems = []
    if not model.objects.filter(pk=obj.pk).exists():
        problems.append(f"{model.__name__} {obj} pk={obj.pk} was deleted, now pks {list(model.objects.filter(**natural_key).values_list('pk', flat=True))}")
    elif str(model.objects.get(pk=obj.pk).resource.ansible_id) != jewel.find(resource_type, **natural_key):
        problems.append(f"{model.__name__} {obj} kept its corrupted ansible_id")
    problems += snapshot.losses() + old_rbac_mismatches() + sync_differences(jewel)
    assert not problems, "\n".join(problems)


@pytest.mark.django_db
def test_pull_in_sync_changes_nothing(world, jewel, pull):
    """Baseline for the fake: a pull right after adopting is a no-op"""
    before = (
        set(Organization.objects.values_list("pk", flat=True)),
        set(Team.objects.values_list("pk", flat=True)),
        set(User.objects.values_list("pk", flat=True)),
    )
    snapshot = AwxOnlySnapshot.take()

    pull()

    assert (
        set(Organization.objects.values_list("pk", flat=True)),
        set(Team.objects.values_list("pk", flat=True)),
        set(User.objects.values_list("pk", flat=True)),
    ) == before
    assert snapshot.losses() == []
    assert sync_differences(jewel) == []
    assert not [c for c in jewel.calls if c.method != "GET"], "a clean pull should only read from Jewel"


@pytest.mark.django_db
@pytest.mark.parametrize("role_name, target", [("Team Admin", "team"), ("Organization Member", "org")])
def test_pull_removes_assignment_jewel_does_not_have(world, jewel, pull, role_name, target):
    """P9: AWX has a role on a shared object that Jewel does not, Jewel is the source of truth so the pull removes it"""
    obj = getattr(world, target)
    with no_reverse_sync():
        RoleDefinition.objects.get(name=role_name).give_permission(world.alice, obj)
    assert RoleUserAssignment.objects.filter(user=world.alice, role_definition__name=role_name, object_id=obj.pk).exists()
    assert not jewel.calls, "the assignment should exist only in AWX"
    snapshot = AwxOnlySnapshot.take()

    pull()

    problems = []
    if RoleUserAssignment.objects.filter(user=world.alice, role_definition__name=role_name, object_id=obj.pk).exists():
        problems.append(f"alice still has {role_name} on {obj}")
    problems += snapshot.losses() + old_rbac_mismatches() + sync_differences(jewel)
    assert not problems, "\n".join(problems)


@pytest.mark.django_db
@pytest.mark.parametrize("target, natural_key", [("org", {"name": "o1"}), ("team", {"name": "t1"}), ("alice", {"username": "alice"})])
@pytest.mark.parametrize("local_change", [False, True], ids=["unchanged", "changed_in_awx"])
def test_pull_gives_jewel_ownership_of_object_awx_marked_local(world, jewel, pull, target, natural_key, local_change):
    """P4: AWX marks a shared object as its own while Jewel has it, Jewel's copy wins on the next pull"""
    obj = getattr(world, target)
    make_local(obj)
    model = type(obj)
    if local_change:
        # An edit made while the object looked local, which reverse sync skips
        model.objects.filter(pk=obj.pk).update(**{"first_name" if model is User else "description": "changed in AWX"})
    snapshot = AwxOnlySnapshot.take()

    pull()

    problems = []
    if not model.objects.filter(pk=obj.pk).exists():
        problems.append(f"{model.__name__} {obj} pk={obj.pk} was deleted")
    problems += snapshot.losses() + old_rbac_mismatches() + sync_differences(jewel)
    assert not problems, "\n".join(problems)
