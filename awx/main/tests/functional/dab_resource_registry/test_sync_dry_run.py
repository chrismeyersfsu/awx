import pytest

from ansible_base.resource_registry.tasks.sync_dry_run import delete_preview

from awx.main.models import Inventory, Organization


@pytest.mark.django_db
@pytest.mark.parametrize("pending_deletion", [False, True], ids=["inventory", "inventory_already_pending_deletion"])
def test_org_delete_preview_lists_inventories_scheduled_for_deletion(organization, pending_deletion):
    """DAB's resource sync dry run is told deleting an org schedules its inventories for deletion

    An inventory already pending deletion is left out, since delete_inventory_for_org cannot
    schedule it again.
    """
    inventory = Inventory.objects.create(name="inv1", organization=organization, pending_deletion=pending_deletion)
    effects = []

    delete_preview.send(sender=Organization, instance=organization, effects=effects)

    assert effects == ([] if pending_deletion else [f"inventory inv1 (pk {inventory.pk}) is scheduled for deletion"])
