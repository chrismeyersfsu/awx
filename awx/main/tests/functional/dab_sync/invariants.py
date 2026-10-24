"""
Checks of AWX's state that tests use to describe what a sync, a login or an API call did.

Each check returns a list of human readable problems, empty when the check holds, so a test can
assert the exact problems it expects and a failure shows every one at once.
"""

import json
from dataclasses import dataclass

from ansible_base.rbac import permission_registry
from ansible_base.rbac.models import RoleTeamAssignment, RoleUserAssignment
from ansible_base.rbac.role_sync_utils import get_local_assignments
from ansible_base.resource_registry.models import Resource

from awx.main.models import ActivityStream, Credential, Inventory, JobTemplate, Organization, Project, Team
from awx.main.models.rbac import ROLE_DEFINITION_TO_ROLE_FIELD

from .fake_jewel import TRACKED_TYPES

AWX_ONLY_MODELS = (Inventory, Project, JobTemplate, Credential)


def old_rbac_mismatches():
    """C2: old style Role membership matches the DAB RBAC assignments it is translated from"""
    problems = []
    for model in (Organization, Team):
        ct = permission_registry.content_type_model.objects.get_for_model(model)
        for obj in model.objects.all():
            for rd_name in ("Organization Member", "Organization Admin") if model is Organization else ("Team Member", "Team Admin"):
                role = getattr(obj, ROLE_DEFINITION_TO_ROLE_FIELD[rd_name])
                old = set(role.members.values_list("id", flat=True))
                new = set(RoleUserAssignment.objects.filter(role_definition__name=rd_name, content_type=ct, object_id=obj.pk).values_list("user_id", flat=True))
                if old != new:
                    problems.append(f"{model.__name__} {obj.name} {rd_name}: old role members {sorted(old)}, assignments {sorted(new)}")

    for assignment in RoleTeamAssignment.objects.filter(role_definition__name__in=ROLE_DEFINITION_TO_ROLE_FIELD).exclude(object_id=None):
        obj = assignment.content_object
        role = getattr(obj, ROLE_DEFINITION_TO_ROLE_FIELD[assignment.role_definition.name], None)
        if obj is not None and role is not None and not role.parents.filter(pk=assignment.team.member_role.pk).exists():
            problems.append(f"team {assignment.team.name} has {assignment.role_definition.name} on {obj} but its member role is not a parent of the old role")
    return problems


@dataclass
class AwxOnlySnapshot:
    """C3: objects only AWX has, and the roles on them, survive whatever sync does to shared objects

    Role holders are recorded by name, so a role re-granted to a recreated user or team is not a loss.
    """

    objects: set
    assignments: set

    @classmethod
    def take(cls):
        objects = set()
        for model in AWX_ONLY_MODELS:
            for obj in model.objects.all():
                objects.add((model.__name__, obj.pk, obj.name, getattr(obj, "organization_id", None)))
        assignments = set()
        for a in RoleUserAssignment.objects.exclude(content_type__service="shared").exclude(object_id=None):
            assignments.add(("user", a.user.username, a.role_definition.name, a.content_type.model, a.object_id))
        for a in RoleTeamAssignment.objects.exclude(content_type__service="shared").exclude(object_id=None):
            assignments.add(("team", a.team.name, a.role_definition.name, a.content_type.model, a.object_id))
        return cls(objects, assignments)

    def losses(self):
        now = self.take()
        return [f"lost {o}" for o in sorted(self.objects - now.objects, key=str)] + [f"lost {a}" for a in sorted(self.assignments - now.assignments, key=str)]


def sync_differences(jewel):
    """C4: after a pull, AWX's shared resources and assignments are Jewel's"""
    problems = []
    for aid, fake in jewel.resources.items():
        local = Resource.objects.filter(ansible_id=aid).select_related("content_type__resource_type").first()
        if local is None:
            problems.append(f"{fake.resource_type} {fake.natural_key()} {aid} is missing in AWX")
            continue
        data = json.loads(json.dumps(local.content_type.resource_type.serializer_class(local.content_object).data))
        if data != fake.resource_data:
            problems.append(f"{fake.resource_type} {aid} differs: AWX {data}, Jewel {fake.resource_data}")
        if str(local.service_id) != fake.service_id:
            problems.append(f"{fake.resource_type} {fake.natural_key()} is owned by {local.service_id} in AWX, by Jewel {fake.service_id} there")
    for local in Resource.objects.filter(content_type__resource_type__name__in=TRACKED_TYPES).exclude(ansible_id__in=list(jewel.resources)):
        problems.append(f"{local.content_type.resource_type.name} {local.name} {local.ansible_id} (service_id {local.service_id}) is only in AWX")

    local_assignments = get_local_assignments()
    problems += [f"assignment only in AWX: {a}" for a in sorted(local_assignments - jewel.assignments, key=str)]
    problems += [f"assignment only in Jewel: {a}" for a in sorted(jewel.assignments - local_assignments, key=str)]
    return problems


@dataclass(frozen=True)
class SyncState:
    """C5: what a pull can change, to check DAB's resource sync dry run against what the pull did

    Resources are (resource type, ansible_id, object_id) and AWX-only objects (model label, pk).
    """

    resources: frozenset
    awx_only: frozenset
    pending_inventories: frozenset
    assignments: frozenset
    activity: int

    @classmethod
    def take(cls):
        return cls(
            resources=frozenset(
                (r.content_type.resource_type.name, str(r.ansible_id), r.object_id) for r in Resource.objects.select_related("content_type__resource_type")
            ),
            awx_only=frozenset((model._meta.label, str(pk)) for model in AWX_ONLY_MODELS for pk in model.objects.values_list("pk", flat=True)),
            pending_inventories=frozenset(Inventory.objects.filter(pending_deletion=True).values_list("pk", flat=True)),
            assignments=frozenset(get_local_assignments()),
            activity=ActivityStream.objects.count(),
        )


def dry_run_mismatches(plan, before, after, results):
    """C5: differences between what the dry run planned and what the pull then did (SyncExecutor.results)"""
    problems = []

    def compare(what, planned, done):
        planned, done = set(planned), set(done)
        if planned != done:
            problems.append(f"{what}: planned but not done {sorted(planned - done, key=str)}, done but not planned {sorted(done - planned, key=str)}")

    compare("deleted shared objects", plan.deleted_ansible_ids, {aid for _, aid, _ in before.resources - after.resources})
    compare("created shared objects", plan.created_ansible_ids, {aid for _, aid, _ in after.resources - before.resources})
    compare("updated shared objects", {c.ansible_id for c in plan.changes if c.action == "update"}, {str(i.ansible_id) for i in results.get("updated", [])})
    compare("deleted AWX-only objects", {o for o in plan.deleted_objects if o[0] in {m._meta.label for m in AWX_ONLY_MODELS}}, before.awx_only - after.awx_only)
    compare(
        "inventories scheduled for deletion",
        {e for c in plan.changes for e in c.detail.get("service effects", [])},
        {
            f"inventory {i.name} (pk {i.pk}) is scheduled for deletion"
            for i in Inventory.objects.filter(pk__in=after.pending_inventories - before.pending_inventories)
        },
    )
    if "assignments_created" in results:
        compare("assignments after the pull", plan.assignments_after, after.assignments)
    return problems
