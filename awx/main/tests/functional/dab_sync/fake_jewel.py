"""
An in-memory Jewel for testing how AWX behaves when its shared state drifts from Jewel's.

It replaces the HTTP call inside DAB's resource server client
(ansible_base.resource_registry.service_client), so everything AWX and DAB do up to the wire
runs for real: service token signing, raise_if_bad_request, the pull, the reverse sync,
assignment pushes and the jwt_claims fetch on a claims hash mismatch.

Changes made in Jewel (create, update, delete, assign, unassign) are pushed to AWX's
/service-index/ endpoints through AWX's real request stack, the way Jewel pushes them.

Responses mirror what Jewel devel returns (its Python package is still aap_gateway_api).
Jewel serves the service index with DAB's own resource_registry and rbac.service_api views,
and builds claims with DAB's claims functions, so the shapes and error messages here are
copied from those.
"""

import csv
import fnmatch
import hashlib
import http
import io
import json
import re
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.db.models import Q
from requests.structures import CaseInsensitiveDict

from ansible_base.rbac.claims import get_claims_hash, get_user_claims_hashable_form
from ansible_base.rbac.models import RoleDefinition
from ansible_base.rbac.role_sync_utils import AssignmentTuple, get_local_assignments
from ansible_base.resource_registry.models import Resource, service_id

JEWEL_URL = "https://jewel.fake.test"
SERVICE_INDEX_PATH = "/api/gateway/v1/service-index/"
JEWEL_API_PATH = "/api/gateway/v1/"
AWX_SERVICE_INDEX_PATH = "/api/controller/v2/service-index/"  # where Jewel reaches AWX's resource and assignment endpoints

# Objects the fake holds and compares with AWX's. It also holds role definitions, those Jewel
# defines (see adopt) and those AWX pushes. Other types (feature flags) get a 404 manifest, which
# the pull skips.
TRACKED_TYPES = ("shared.organization", "shared.team", "shared.user")
ROLE_DEFINITION = "shared.roledefinition"
NATURAL_KEYS = {
    "shared.organization": ("name",),
    "shared.team": ("name", "organization"),
    "shared.user": ("username",),
    ROLE_DEFINITION: ("name",),
}
OBJECT_TYPES = {"shared.organization": "organization", "shared.team": "team"}
# Fields Jewel's serializers fill in when a create leaves them out
RESOURCE_DEFAULTS = {
    "shared.organization": {"description": ""},
    "shared.team": {"description": ""},
    "shared.user": {"email": "", "first_name": "", "last_name": "", "is_superuser": False},
}


@dataclass
class FakeResource:
    resource_type: str
    service_id: str
    resource_data: dict

    def natural_key(self):
        return tuple(self.resource_data.get(f) for f in NATURAL_KEYS[self.resource_type])

    def resource_hash(self):
        # Same as SharedResourceTypeSerializer.get_hash, so an unchanged resource is a NOOP in the pull
        return hashlib.sha256(json.dumps(self.resource_data, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass
class Call:
    method: str
    path: str
    data: dict | None
    params: dict | None
    status: int | None = None


@dataclass
class Push:
    method: str
    path: str
    data: dict | None
    status: int | None = None
    body: dict | str | None = None
    lost: bool = False


class JewelRefused(Exception):
    """Jewel refused a change made in it, with this body and status"""

    def __init__(self, body, status=400):
        super().__init__(f"{status} {body}")
        self.body, self.status = body, status


@dataclass
class Failure:
    method: str
    path_glob: str
    status: int | None
    body: dict | str | None
    exc: Exception | None
    times: int | None
    headers: dict | None = None

    def matches(self, method, path):
        return self.times != 0 and self.method in ("*", method) and fnmatch.fnmatch(path, self.path_glob)


@dataclass
class FakeJewel:
    service_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resources: dict[str, FakeResource] = field(default_factory=dict)
    assignments: set[AssignmentTuple] = field(default_factory=set)
    role_definitions: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)
    unhandled: list[Call] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    pushes: list[Push] = field(default_factory=list)
    awx: object = None  # django.test.Client the pushes go through, set by the jewel fixture
    _losing_pushes: bool = False
    _holding_pushes: bool = False
    _held: list = field(default_factory=list)

    def __post_init__(self):
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = (
            self._private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode().strip()
        )
        self._routes = [
            ("GET", rf"{SERVICE_INDEX_PATH}metadata/", self._metadata),
            ("GET", rf"{SERVICE_INDEX_PATH}resource-types/(?P<name>[^/]+)/manifest/", self._manifest),
            ("GET", rf"{SERVICE_INDEX_PATH}resources/(?P<ansible_id>[^/]+)/", self._get_resource),
            ("POST", rf"{SERVICE_INDEX_PATH}resources/", self._create_resource),
            ("PATCH", rf"{SERVICE_INDEX_PATH}resources/(?P<ansible_id>[^/]+)/", self._update_resource),
            ("PUT", rf"{SERVICE_INDEX_PATH}resources/(?P<ansible_id>[^/]+)/", self._update_resource),
            ("DELETE", rf"{SERVICE_INDEX_PATH}resources/(?P<ansible_id>[^/]+)/", self._delete_resource),
            ("GET", rf"{SERVICE_INDEX_PATH}role-(?P<actor>user|team)-assignments/", self._list_assignments),
            ("POST", rf"{SERVICE_INDEX_PATH}role-(?P<actor>user|team)-assignments/(?P<action>assign|unassign)/", self._assign),
            ("POST", rf"{SERVICE_INDEX_PATH}role-(?P<actor>user|team)-assignments/bulk-(?P<action>assign|unassign)/", self._bulk_assign),
            ("POST", rf"{SERVICE_INDEX_PATH}object-delete/", self._object_delete),
            ("GET", rf"{JEWEL_API_PATH}jwt_claims/(?P<ansible_id>[^/]+)/", self._jwt_claims),
        ]

    # ---- setting up state ----

    def adopt(self):
        """Make the fake hold exactly AWX's shared state, owned by Jewel: the state right after a clean sync

        Of the role definitions, Jewel holds and owns those for orgs, teams and the whole system,
        AWX keeps owning those for its own objects (not verified against Jewel).
        """
        self.resources.clear()
        platform_role_definitions = RoleDefinition.objects.filter(Q(content_type=None) | Q(content_type__model__in=OBJECT_TYPES.values()))
        resources = Resource.objects.filter(
            Q(content_type__resource_type__name__in=TRACKED_TYPES)
            | Q(content_type__resource_type__name=ROLE_DEFINITION, object_id__in=[str(pk) for pk in platform_role_definitions.values_list("pk", flat=True)])
        ).select_related("content_type__resource_type")
        for resource in resources:
            resource_type = resource.content_type.resource_type
            data = json.loads(json.dumps(resource_type.serializer_class(resource.content_object).data))
            self.resources[str(resource.ansible_id)] = FakeResource(resource_type.name, self.service_id, data)
        resources.update(service_id=self.service_id)
        # Migrations create AWX's role definitions without pushing them, so they are AWX's own
        Resource.objects.filter(content_type__resource_type__name=ROLE_DEFINITION).exclude(pk__in=resources.values("pk")).update(service_id=service_id())
        self.assignments = get_local_assignments()
        self.role_definitions = set(RoleDefinition.objects.values_list("name", flat=True))

    def find(self, resource_type, **natural_key):
        """ansible_id of the resource matching natural key fields, like find("shared.user", username="alice")"""
        matches = [
            aid for aid, r in self.resources.items() if r.resource_type == resource_type and all(r.resource_data.get(k) == v for k, v in natural_key.items())
        ]
        if len(matches) != 1:
            raise LookupError(f"expected one {resource_type} matching {natural_key}, found {len(matches)}")
        return matches[0]

    # ---- Jewel's own API: changes made in Jewel, pushed to AWX ----
    #
    # Jewel pushes every change to an org, team, user, role definition or assignment to each
    # service (aap_gateway_api.views.api.v1.common.ResourceAPIUpdateMixin and role.AssignmentSyncMixin)
    # in the background, as the user who made it with resource_api_actions "*". It only logs a
    # failed push and relies on each service's periodic pull to catch up.

    def create(self, resource_type, by=None, **resource_data):
        """Create a resource in Jewel and push it, returning its ansible_id"""
        resource_data = {**RESOURCE_DEFAULTS.get(resource_type, {}), **resource_data}
        ansible_id = str(uuid.uuid4())
        self._refuse_duplicate(ansible_id, FakeResource(resource_type, self.service_id, resource_data))
        self.resources[ansible_id] = FakeResource(resource_type, self.service_id, resource_data)
        if resource_type == ROLE_DEFINITION:
            self.role_definitions.add(resource_data["name"])
        self._push(
            "POST", "resources/", by, {"ansible_id": ansible_id, "service_id": self.service_id, "resource_type": resource_type, "resource_data": resource_data}
        )
        return ansible_id

    def update(self, ansible_id, by=None, **changes):
        """Change a resource in Jewel and push its new data"""
        resource = self.resources[ansible_id]
        self._refuse_managed(resource)
        self._refuse_duplicate(ansible_id, FakeResource(resource.resource_type, resource.service_id, {**resource.resource_data, **changes}))
        if resource.resource_type == ROLE_DEFINITION and "name" in changes:
            old, new = resource.resource_data["name"], changes["name"]
            self.role_definitions = (self.role_definitions - {old}) | {new}
            self.assignments = {replace(a, role_definition_name=new) if a.role_definition_name == old else a for a in self.assignments}
        resource.resource_data = {**resource.resource_data, **changes}
        self._push("PUT", f"resources/{ansible_id}/", by, {"resource_data": resource.resource_data})

    def delete(self, ansible_id, by=None):
        """Delete a resource in Jewel, with its assignments, and push the delete

        Deleting an org deletes its teams in Jewel too. Jewel pushes only the org's delete:
        each service cascades on its own.
        """
        resource = self.resources[ansible_id]
        self._refuse_managed(resource)
        del self.resources[ansible_id]
        if resource.resource_type == "shared.organization":
            for team in [aid for aid, r in self.resources.items() if r.resource_type == "shared.team" and r.resource_data.get("organization") == ansible_id]:
                del self.resources[team]
                self.assignments = {a for a in self.assignments if team not in (a.actor_ansible_id, a.ansible_id_or_pk)}
        if resource.resource_type == ROLE_DEFINITION:
            name = resource.resource_data["name"]
            self.role_definitions.discard(name)
            self.assignments = {a for a in self.assignments if a.role_definition_name != name}
        self.assignments = {a for a in self.assignments if ansible_id not in (a.actor_ansible_id, a.ansible_id_or_pk)}
        self._push("DELETE", f"resources/{ansible_id}/", by)

    def assign(self, role, actor, obj, assignment_type="user", by=None):
        """Give a user or team (by ansible_id) a role in Jewel and push it

        obj is the ansible_id of an org or team, or an AWX object. A role on an AWX object belongs
        to AWX, so Jewel sends it to AWX first and fails, with AWX's answer, if AWX rejects it.
        """
        self._change_assignment("assign", role, actor, obj, assignment_type, by)

    def unassign(self, role, actor, obj, assignment_type="user", by=None):
        """Take a role away in Jewel and push it, the same way as assign"""
        self._change_assignment("unassign", role, actor, obj, assignment_type, by)

    def pushing(self, delivered):
        """A context where Jewel's pushes are delivered, or lost (see pushes_lost)"""
        return nullcontext() if delivered else self.pushes_lost()

    @contextmanager
    def pushes_lost(self):
        """Jewel's pushes inside never reach AWX, as when AWX is down or the push times out"""
        self._losing_pushes = True
        try:
            yield
        finally:
            self._losing_pushes = False

    @contextmanager
    def pushes_held(self):
        """Jewel's pushes inside wait until deliver_held(), as pushes from Jewel's background threads can arrive late or out of order"""
        self._holding_pushes = True
        try:
            yield
        finally:
            self._holding_pushes = False

    def deliver_held(self):
        """Send the pushes held by pushes_held(), in the order they were made"""
        held, self._held = self._held, []
        for push, by in held:
            self._send(push, by)

    def replace_user(self, ansible_id, by=None):
        """Delete a user in Jewel and register a different person under the same username, returning the new ansible_id"""
        old = self.resources[ansible_id]
        self.delete(ansible_id, by=by)
        return self.create("shared.user", by=by, **dict(old.resource_data, first_name="Someone", last_name="Else"))

    def _refuse_duplicate(self, ansible_id, resource):
        if self._natural_key_clash(ansible_id, resource):
            raise JewelRefused({"detail": f"{resource.resource_type} {resource.natural_key()} already exists"})

    def _refuse_managed(self, resource):
        if resource.resource_type == ROLE_DEFINITION and resource.resource_data.get("managed"):
            raise JewelRefused({"detail": "Role is managed by the system"})

    def _change_assignment(self, action, role, actor, obj, assignment_type, by):
        shared = isinstance(obj, str)
        body = {"role_definition": role, f"{assignment_type}_ansible_id": actor, "from_service": self.service_id}
        body["object_ansible_id" if shared else "object_id"] = obj if shared else str(obj.pk)
        assignment = AssignmentTuple(actor, obj if shared else str(obj.pk), role, assignment_type)
        if shared:
            self._set_assignment(action, assignment)
            self._push("POST", f"role-{assignment_type}-assignments/{action}/", by, body)
            return
        push = self._push("POST", f"role-{assignment_type}-assignments/{action}/", by, body)
        if push.lost or not 200 <= push.status < 300:
            raise JewelRefused(push.body, push.status)
        self._set_assignment(action, assignment)

    def _set_assignment(self, action, assignment):
        if action == "assign":
            self.assignments.add(assignment)
        else:
            self.assignments.discard(assignment)

    def _push(self, method, path, by, data=None):
        push = Push(method, path, data)
        self.pushes.append(push)
        if self._losing_pushes:
            push.lost = True
            return push
        by = by or self._default_user()
        if self._holding_pushes:
            self._held.append((push, by))
            return push
        return self._send(push, by)

    def _send(self, push, by):
        send = getattr(self.awx, push.method.lower())
        response = send(
            AWX_SERVICE_INDEX_PATH + push.path,
            data=json.dumps(push.data) if push.data is not None else None,
            content_type="application/json",
            **self.jwt_headers(by, resource_api_actions="*"),
        )
        push.status = response.status_code
        try:
            push.body = json.loads(response.content) if response.content else None
        except ValueError:
            push.body = response.content.decode()[:500]
        return push

    def _default_user(self):
        return next(aid for aid, r in self.resources.items() if r.resource_type == "shared.user" and r.resource_data.get("is_superuser"))

    def fail(self, method, path_glob, status=None, body=None, exc=None, times=None, headers=None):
        """Answer matching requests with a status, body and headers instead of the route, or raise exc (like requests.ConnectionError)"""
        self.failures.append(Failure(method, path_glob, status, body, exc, times, headers))

    # ---- what Jewel hands a user ----

    def claims(self, user_ansible_id):
        """Claims shaped like ansible_base.rbac.claims.get_user_claims, from the fake's assignments"""
        objects = {"organization": [], "team": []}
        index = {"organization": {}, "team": {}}
        object_roles = {}
        global_roles = []
        managed = set(settings.ANSIBLE_BASE_JWT_MANAGED_ROLES)

        def add_object(model_type, aid):
            if aid not in index[model_type]:
                index[model_type][aid] = len(objects[model_type])
                objects[model_type].append({"ansible_id": aid, "name": self.resources[aid].resource_data["name"]})
            return index[model_type][aid]

        for a in sorted(self.assignments, key=str):
            if a.assignment_type != "user" or a.actor_ansible_id != user_ansible_id or a.role_definition_name not in managed:
                continue
            if a.ansible_id_or_pk is None:
                global_roles.append(a.role_definition_name)
                continue
            obj = self.resources.get(a.ansible_id_or_pk)
            if obj is None or obj.resource_type not in OBJECT_TYPES:
                continue
            model_type = OBJECT_TYPES[obj.resource_type]
            role = object_roles.setdefault(a.role_definition_name, {"content_type": model_type, "objects": []})
            role["objects"].append(add_object(model_type, a.ansible_id_or_pk))

        for team in objects["team"]:
            org_aid = self.resources[team["ansible_id"]].resource_data.get("organization")
            if org_aid in self.resources:
                team["org"] = add_object("organization", org_aid)
        return {"objects": objects, "object_roles": object_roles, "global_roles": global_roles}

    def jwt(self, user_ansible_id, **overrides):
        """A token shaped like aap_gateway_api.utils.jwt_token.create_signed_jwt"""
        user = self.resources[user_ansible_id].resource_data
        payload = {
            "version": "1",
            "iss": "ansible-issuer",
            "exp": int((datetime.now() + timedelta(minutes=10)).timestamp()),
            "aud": "ansible-services",
            "sub": user_ansible_id,
            "service_id": self.service_id,
            "user_data": {k: user.get(k, "") for k in ("username", "first_name", "last_name", "email")} | {"is_superuser": user.get("is_superuser", False)},
            "claims_hash": get_claims_hash(get_user_claims_hashable_form(self.claims(user_ansible_id))),
            **overrides,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def jwt_headers(self, user_ansible_id, **overrides):
        """Request headers for django.test.Client, the way Jewel proxies a logged in user"""
        return {"HTTP_X_DAB_JW_TOKEN": self.jwt(user_ansible_id, **overrides)}

    # ---- the wire ----

    def request(self, method, url, **kwargs):
        """Stands in for requests.request inside DAB's service client"""
        method = method.upper()
        path = urlparse(url).path
        data, params = kwargs.get("json"), kwargs.get("params")
        call = Call(method, path, data, params)
        self.calls.append(call)

        for failure in self.failures:
            if failure.matches(method, path):
                if failure.times is not None:
                    failure.times -= 1
                if failure.exc is not None:
                    raise failure.exc
                return self._response(call, url, failure.status, failure.body, failure.headers)

        for verb, pattern, handler in self._routes:
            if verb == method and (match := re.fullmatch(pattern, path)):
                status, body, headers = handler(data or {}, params or {}, **match.groupdict())
                return self._response(call, url, status, body, headers)

        self.unhandled.append(call)
        return self._response(call, url, 501, {"detail": f"fake Jewel has no route for {method} {path}"})

    def _response(self, call, url, status, body, headers=None):
        call.status = status
        response = requests.Response()
        response.status_code = status
        response.reason = http.HTTPStatus(status).phrase
        response.url = url + (f"?{urlencode(call.params)}" if call.params else "")
        response.encoding = "utf-8"
        response.headers = CaseInsensitiveDict(headers or {})
        if isinstance(body, str):
            response._content = body.encode()
        elif body is not None:
            response._content = json.dumps(body).encode()
            response.headers.setdefault("Content-Type", "application/json")
        else:
            response._content = b""
        response.raw = io.BytesIO(response._content)  # read already, like a real response once its content is accessed
        response._content_consumed = True
        response.request = requests.Request(call.method, response.url).prepare()
        return response

    # ---- service index ----

    def _resource_body(self, ansible_id):
        r = self.resources[ansible_id]
        return {
            "ansible_id": ansible_id,
            "service_id": r.service_id,
            "is_partially_migrated": False,
            "resource_type": r.resource_type,
            "has_serializer": True,
            "resource_data": r.resource_data,
            "name": r.resource_data.get("name", r.resource_data.get("username")),
            "url": f"{SERVICE_INDEX_PATH}resources/{ansible_id}/",
            "additional_data": None,
        }

    def _metadata(self, data, params):
        return 200, {"service_id": self.service_id, "service_type": "aap"}, None

    def _manifest(self, data, params, name):
        # Jewel's own resources plus the asking service's (resource_registry.views.ResourceTypeViewSet.manifest)
        wanted = {self.service_id, params.get("service_id")}
        rows = [(aid, r.resource_hash()) for aid, r in self.resources.items() if r.resource_type == name and r.service_id in wanted]
        if not rows:
            return 404, None, None
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(("ansible_id", "resource_hash"))
        writer.writerows(rows)
        return 200, out.getvalue(), {"X-Resource-Count": str(len(rows)), "Content-Type": "text/csv"}

    def _get_resource(self, data, params, ansible_id):
        if ansible_id not in self.resources:
            return 404, {"detail": "No Resource matches the given query."}, None
        return 200, self._resource_body(ansible_id), None

    def _create_resource(self, data, params):
        resource_type = data.get("resource_type")
        if resource_type == ROLE_DEFINITION:
            self.role_definitions.add(data["resource_data"]["name"])
        if resource_type not in NATURAL_KEYS:
            return 201, {**data, "service_id": self.service_id}, None
        new = FakeResource(resource_type, self.service_id, data.get("resource_data", {}))
        ansible_id = str(data.get("ansible_id") or uuid.uuid4())
        if clash := self._natural_key_clash(ansible_id, new):
            return clash
        self.resources[ansible_id] = new
        return 201, self._resource_body(ansible_id), None

    def _natural_key_clash(self, ansible_id, new):
        for aid, r in self.resources.items():
            if aid != ansible_id and r.resource_type == new.resource_type and r.natural_key() == new.natural_key():
                # Not verified against Jewel: a natural key clash is a database integrity error there
                return 400, {"detail": f"{new.resource_type} {new.natural_key()} already exists"}, None
        return None

    def _update_resource(self, data, params, ansible_id):
        if ansible_id not in self.resources:
            return 404, {"detail": "No Resource matches the given query."}, None
        resource = self.resources[ansible_id]
        updated = FakeResource(resource.resource_type, resource.service_id, {**resource.resource_data, **data.get("resource_data", {})})
        if clash := self._natural_key_clash(ansible_id, updated):
            return clash
        resource.resource_data = updated.resource_data
        return 200, self._resource_body(ansible_id), None

    def _delete_resource(self, data, params, ansible_id):
        if ansible_id not in self.resources:
            return 404, {"detail": "No Resource matches the given query."}, None
        del self.resources[ansible_id]
        self.assignments = {a for a in self.assignments if ansible_id not in (a.actor_ansible_id, a.ansible_id_or_pk)}
        return 204, None, None

    # ---- role assignments (rbac.service_api) ----

    def _assignment_body(self, a):
        obj = self.resources.get(a.ansible_id_or_pk) if a.ansible_id_or_pk else None
        return {
            f"{a.assignment_type}_ansible_id": a.actor_ansible_id,
            "role_definition": a.role_definition_name,
            "object_ansible_id": a.ansible_id_or_pk if obj else None,
            "object_id": a.ansible_id_or_pk,
        }

    def _list_assignments(self, data, params, actor):
        actor_filter = params.get(f"{actor}_ansible_id")
        rows = sorted((a for a in self.assignments if a.assignment_type == actor and actor_filter in (None, a.actor_ansible_id)), key=str)
        page, page_size = int(params.get("page", 1)), int(params.get("page_size", 25))
        chunk = rows[(page - 1) * page_size : page * page_size]
        more = page * page_size < len(rows)
        return (
            200,
            {"count": len(rows), "next": f"?page={page + 1}" if more else None, "previous": None, "results": [self._assignment_body(a) for a in chunk]},
            None,
        )

    def _validate_assignment(self, item, actor):
        """Field errors the way DRF reports them for ServiceRoleUserAssignmentSerializer and the team variant"""
        errors = {}
        actor_id = item.get(f"{actor}_ansible_id")
        if self.resources.get(actor_id, FakeResource("", "", {})).resource_type != f"shared.{actor}":
            errors[f"{actor}_ansible_id"] = [f"No {actor} found with ansible_id={actor_id}"]
        if item.get("role_definition") not in self.role_definitions:
            errors["role_definition"] = [f"Object with name={item.get('role_definition')} does not exist."]
        object_ansible_id = item.get("object_ansible_id")
        if object_ansible_id and object_ansible_id not in self.resources:
            errors["object_ansible_id"] = ["Resource with this ansible_id does not exist."]
        target = object_ansible_id or item.get("object_id")
        return errors, AssignmentTuple(actor_id, str(target) if target else None, item.get("role_definition"), actor)

    def _assign(self, data, params, actor, action):
        errors, a = self._validate_assignment(data, actor)
        if errors:
            return 400, errors, None
        if action == "assign":
            if a in self.assignments:
                return 200, self._assignment_body(a), None
            self.assignments.add(a)
            return 201, self._assignment_body(a), None
        if a not in self.assignments:
            return 200, None, None
        self.assignments.discard(a)
        return 204, None, None

    def _bulk_assign(self, data, params, actor, action):
        checked = [self._validate_assignment(item, actor) for item in data.get("assignments", [])]
        if any(errors for errors, _ in checked):
            return 400, {"assignments": [errors for errors, _ in checked]}, None
        triples = {a for _, a in checked}
        if action == "assign":
            created = triples - self.assignments
            self.assignments |= created
            return 200, {"created": len(created), "existing": len(triples) - len(created)}, None
        deleted = triples & self.assignments
        self.assignments -= deleted
        return 200, {"deleted": len(deleted), "missing": len(triples) - len(deleted)}, None

    def _object_delete(self, data, params):
        pk = data.get("resource_pk")
        doomed = {a for a in self.assignments if a.ansible_id_or_pk == pk and a.ansible_id_or_pk not in self.resources}
        self.assignments -= doomed
        return 200, {"deleted_count": len(doomed)}, None

    # ---- jewel API ----

    def _jwt_claims(self, data, params, ansible_id):
        # aap_gateway_api.views.api.v1.jwt_claims.JWTClaimsView
        if self.resources.get(ansible_id, FakeResource("", "", {})).resource_type != "shared.user":
            return 404, {"error": f"User with ansible_id {ansible_id} not found"}, None
        claims = self.claims(ansible_id)
        claims["claims_hash"] = get_claims_hash(get_user_claims_hashable_form(claims))
        return 200, claims, None
