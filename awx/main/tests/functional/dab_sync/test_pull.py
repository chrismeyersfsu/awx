import pytest

from awx.main.models import Organization, Team, User

from .drift import corrupt_ansible_id
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
