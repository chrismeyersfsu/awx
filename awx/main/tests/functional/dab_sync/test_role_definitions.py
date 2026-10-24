"""
How AWX follows today when Jewel creates, renames or deletes a role definition, and what happens to the Compat role definitions AWX makes.

These tests record current behavior, they do not judge it.

Role definitions are shared resources: Jewel pushes its changes to them, and the pull syncs
them like orgs and teams. Managed role definitions (Team Member, Organization Admin, ...) cannot
be renamed or deleted, so these use a custom one.
"""

import pytest

from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment

from .fake_jewel import ROLE_DEFINITION
from .invariants import sync_differences

DELIVERY = pytest.mark.parametrize("delivered", [True, False], ids=["pushed", "push_lost"])
VIEWER = {"description": "", "managed": False, "permissions": ["awx.view_inventory", "shared.view_organization"], "content_type": "shared.organization"}


def _has_role(user, role_name, obj):
    return RoleUserAssignment.objects.filter(user=user, role_definition__name=role_name, object_id=obj.pk).exists()


def _viewer_for_alice(jewel, world):
    """Jewel creates the custom org role "Inventory Viewer" and gives it to alice on o1"""
    rd = jewel.create(ROLE_DEFINITION, name="Inventory Viewer", **VIEWER)
    jewel.assign("Inventory Viewer", jewel.find("shared.user", username="alice"), str(world.org.resource.ansible_id))
    return rd


def _alice_sees_inv1(api, jewel, world):
    return api.get(f"/api/controller/v2/inventories/{world.inventory.pk}/", **jewel.jwt_headers(jewel.find("shared.user", username="alice"))).status_code


@pytest.mark.django_db
@DELIVERY
def test_jewel_creates_custom_role_definition(world, jewel, api, pull, delivered):
    """P10: Jewel creates a custom org role and gives it to alice on o1

    Pushed, AWX creates the role definition, owned by Jewel, and the assignment, and alice can
    see o1's inventory. With the pushes lost, the pull creates both.
    """
    with jewel.pushing(delivered):
        rd = _viewer_for_alice(jewel, world)
    assert RoleDefinition.objects.filter(name="Inventory Viewer").exists() is delivered

    pull()

    assert str(RoleDefinition.objects.get(name="Inventory Viewer").resource.ansible_id) == rd
    assert str(RoleDefinition.objects.get(name="Inventory Viewer").resource.service_id) == jewel.service_id
    assert _has_role(world.alice, "Inventory Viewer", world.org)
    assert _alice_sees_inv1(api, jewel, world) == 200
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@DELIVERY
def test_jewel_renames_custom_role_definition(world, jewel, pull, delivered):
    """P10: Jewel renames the custom role, AWX renames its role definition and alice keeps the assignment, by push or else by the pull"""
    rd = _viewer_for_alice(jewel, world)
    with jewel.pushing(delivered):
        jewel.update(rd, name="Org Inventory Viewer")
    assert RoleDefinition.objects.filter(name="Org Inventory Viewer").exists() is delivered

    pull()

    assert not RoleDefinition.objects.filter(name="Inventory Viewer").exists()
    assert _has_role(world.alice, "Org Inventory Viewer", world.org)
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@DELIVERY
def test_jewel_deletes_custom_role_definition(world, jewel, api, pull, delivered):
    """P10: Jewel deletes the custom role, AWX deletes its role definition with alice's assignment, by push or else by the pull"""
    rd = _viewer_for_alice(jewel, world)
    with jewel.pushing(delivered):
        jewel.delete(rd)
    assert RoleDefinition.objects.filter(name="Inventory Viewer").exists() is not delivered

    pull()

    assert not RoleDefinition.objects.filter(name="Inventory Viewer").exists()
    assert not RoleUserAssignment.objects.filter(user=world.alice, content_type__model="organization").exists()
    assert _alice_sees_inv1(api, jewel, world) in (403, 404)
    assert sync_differences(jewel) == []


@pytest.mark.django_db
@DELIVERY
def test_jewel_deletes_compat_role_definition(world, jewel, pull, delivered):
    """P10: adding bob to jt1's old read_role makes AWX create JobTemplate Read Compat, which has no managed equivalent

    AWX pushes the Compat role definition and the assignment to Jewel, which then owns the role
    definition. When Jewel deletes it, AWX deletes it with bob's assignment (by push, or else by
    the pull), but bob stays a member of the old read_role.
    """
    world.jt.read_role.members.add(world.bob)
    compat = jewel.find(ROLE_DEFINITION, name="JobTemplate Read Compat")
    assert str(RoleDefinition.objects.get(name="JobTemplate Read Compat").resource.service_id) == jewel.service_id
    with jewel.pushing(delivered):
        jewel.delete(compat)

    pull()

    assert not RoleDefinition.objects.filter(name="JobTemplate Read Compat").exists()
    assert not _has_role(world.bob, "JobTemplate Read Compat", world.jt)
    assert world.jt.read_role.members.filter(pk=world.bob.pk).exists()
    assert sync_differences(jewel) == []
