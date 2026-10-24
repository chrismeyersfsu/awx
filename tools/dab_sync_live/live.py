"""
Drive a real AAP (Jewel and AWX) to check the dab_sync characterization rows against it.

Jewel's API makes changes the way users do. Drift is injected, and AWX's state read, with
awx-manage shell inside the AWX container over ssh. The instance's addresses and admin
password come from instance.json (mode 0600, never printed).
"""

import json
import os
import subprocess
import textwrap
import time
import uuid

import requests
import urllib3

urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
INSTANCE = json.load(open(os.path.join(HERE, "instance.json")))
JEWEL = INSTANCE["jewel_url"].rstrip("/")
ADMIN = ("admin", INSTANCE["admin_password"])
SSH = [
    "ssh",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-i",
    os.path.expanduser(INSTANCE.get("ssh_key", "~/.ssh/id_rsa")),
]
AWX_CONTAINER = INSTANCE.get("awx_container", "automation-controller-web")
JEWEL_CONTAINER = INSTANCE.get("jewel_container", "automation-gateway")


def call(method, path, auth=ADMIN, json_body=None, params=None, headers=None):
    """A request to Jewel: /api/gateway/v1/... or, proxied, /api/controller/v2/..."""
    r = requests.request(method, JEWEL + path, auth=auth, json=json_body, params=params, headers=headers, verify=False, timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    return r.status_code, body


def jewel(method, path, **kw):
    return call(method, f"/api/gateway/v1/{path.lstrip('/')}", **kw)


def awx_api(method, path, **kw):
    return call(method, f"/api/controller/v2/{path.lstrip('/')}", **kw)


def ok(result, *statuses):
    status, body = result
    assert status in (statuses or (200, 201, 204)), f"{status} {body}"
    return body


def _container_python(container, manage, code):
    script = "import json\n" + textwrap.dedent(code) + "\n"
    cmd = SSH + [f"{INSTANCE.get('ssh_user', 'ec2-user')}@{INSTANCE['aio_host']}", f"podman exec -i {container} {manage} shell"]
    proc = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=900)
    out = [line[2:] for line in proc.stdout.splitlines() if line.startswith("@@")]
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"{container} shell failed ({proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return json.loads(out[-1])


def awx(code):
    """Run python in AWX (awx-manage shell); the code must print('@@' + json.dumps(result)) last"""
    return _container_python(AWX_CONTAINER, "awx-manage", code)


def jewel_shell(code):
    """Run python in Jewel (aap-gateway-manage shell), same protocol as awx()"""
    return _container_python(JEWEL_CONTAINER, "aap-gateway-manage", code)


def pull():
    """AWX's periodic resource sync, run now the way the dispatcher runs it"""
    return awx(
        """
        from awx.main.tasks.system import periodic_resource_sync
        try:
            periodic_resource_sync()
            print('@@' + json.dumps({'raised': None}))
        except Exception as e:
            print('@@' + json.dumps({'raised': f'{type(e).__name__}: {e}'[:300]}))
        """
    )


def wait(predicate, timeout=60, every=3):
    """Wait for Jewel's asynchronous pushes to land"""
    end = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() > end:
            return value
        time.sleep(every)


def suffix():
    return uuid.uuid4().hex[:6]


# ---- Jewel objects ----


def jewel_id(kind, **filters):
    body = ok(jewel("GET", f"{kind}/", params=filters))
    assert body["count"] == 1, f"{kind} {filters}: {body['count']}"
    return body["results"][0]["id"]


def jewel_ansible_id(kind, pk):
    return ok(jewel("GET", f"{kind}/{pk}/"))["summary_fields"]["resource"]["ansible_id"]


def jewel_org(name):
    return ok(jewel("POST", "organizations/", json_body={"name": name}))["id"]


def jewel_team(name, org):
    return ok(jewel("POST", "teams/", json_body={"name": name, "organization": org}))["id"]


def jewel_user(username, password, **extra):
    return ok(jewel("POST", "users/", json_body={"username": username, "password": password, **extra}))["id"]


def jewel_rd(name):
    return jewel_id("role_definitions", name=name)


def jewel_assign_user(role, user, object_id):
    return jewel("POST", "role_user_assignments/", json_body={"role_definition": jewel_rd(role), "user": user, "object_id": object_id})


def jewel_assign_team(role, team, object_id):
    return jewel("POST", "role_team_assignments/", json_body={"role_definition": jewel_rd(role), "team": team, "object_id": object_id})


def jewel_unassign_user(role, user, object_id):
    body = ok(jewel("GET", "role_user_assignments/", params={"role_definition": jewel_rd(role), "user": user, "object_id": object_id}))
    return [jewel("DELETE", f"role_user_assignments/{a['id']}/") for a in body["results"]]


# ---- AWX state ----

AWX_STATE = """
from ansible_base.rbac.models import RoleUserAssignment, RoleTeamAssignment
from ansible_base.resource_registry.models import Resource, service_id
from awx.main.models import Organization, Team, User, Inventory, Credential, JobTemplate
names = {names!r}
def res(o):
    try:
        r = Resource.get_resource_for_object(o)
        return [str(r.ansible_id), 'local' if str(r.service_id) == service_id() else str(r.service_id)]
    except Resource.DoesNotExist:
        return None
state = {{
    'orgs': {{o.name: [o.pk, res(o)] for o in Organization.objects.filter(name__in=names)}},
    'teams': {{t.name: [t.pk, t.organization.name, res(t)] for t in Team.objects.filter(name__in=names)}},
    'users': {{u.username: [u.pk, res(u), u.is_superuser, u.first_name] for u in User.objects.filter(username__in=names)}},
    'inventories': {{i.name: [i.pk, i.organization.name if i.organization else None] for i in Inventory.objects.filter(name__in=names)}},
    'credentials': {{c.name: [c.pk, c.organization.name if c.organization else None] for c in Credential.objects.filter(name__in=names)}},
    'user_roles': sorted([a.user.username, a.role_definition.name, str(a.content_object) if a.content_object else None] for a in RoleUserAssignment.objects.filter(user__username__in=names)),
    'team_roles': sorted([a.team.name, a.role_definition.name, str(a.content_object) if a.content_object else None] for a in RoleTeamAssignment.objects.filter(team__name__in=names)),
    'old_members': sorted([type(r.content_object).__name__, str(r.content_object), r.role_field, u] for u in names for r in __import__('awx.main.models', fromlist=['Role']).Role.objects.filter(members__username=u, role_field__in=['member_role', 'admin_role', 'read_role', 'execute_role', 'use_role', 'inventory_admin_role']) if r.content_object is not None and getattr(r.content_object, 'name', None) in names),
}}
print('@@' + json.dumps(state, default=str))
"""


def awx_state(*names):
    return awx(AWX_STATE.format(names=list(names)))


def corrupt(model, name, field="name"):
    """Give an AWX object's Resource an ansible_id Jewel has never seen, returning it"""
    return awx(
        f"""
        import uuid
        from django.contrib.contenttypes.models import ContentType
        from ansible_base.resource_registry.models import Resource
        from awx.main.models import {model}
        obj = {model}.objects.get({field}={name!r})
        new = str(uuid.uuid4())
        assert Resource.objects.filter(content_type=ContentType.objects.get_for_model(obj), object_id=str(obj.pk)).update(ansible_id=new) == 1
        print('@@' + json.dumps(new))
        """
    )


def set_ansible_id(model, name, ansible_id, field="name"):
    return awx(
        f"""
        from django.contrib.contenttypes.models import ContentType
        from ansible_base.resource_registry.models import Resource
        from awx.main.models import {model}
        obj = {model}.objects.get({field}={name!r})
        assert Resource.objects.filter(content_type=ContentType.objects.get_for_model(obj), object_id=str(obj.pk)).update(ansible_id={ansible_id!r}) == 1
        print('@@' + json.dumps(True))
        """
    )


def make_local(model, name, field="name"):
    return awx(
        f"""
        from django.contrib.contenttypes.models import ContentType
        from ansible_base.resource_registry.models import Resource, service_id
        from awx.main.models import {model}
        obj = {model}.objects.get({field}={name!r})
        assert Resource.objects.filter(content_type=ContentType.objects.get_for_model(obj), object_id=str(obj.pk)).update(service_id=service_id()) == 1
        print('@@' + json.dumps(True))
        """
    )
