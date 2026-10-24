"""
The dab_sync rows, run against a real AAP. Each scenario builds its own world through Jewel
(like the tests' world fixture) and records what happens, for comparison with the fake's result.

    uv run --with requests python scenarios.py [scenario ...]
"""

import json
import os
import sys
import time
import traceback
from types import SimpleNamespace

from live import (
    awx,
    awx_state,
    corrupt,
    awx_api,
    jewel,
    jewel_ansible_id,
    jewel_assign_team,
    jewel_assign_user,
    jewel_org,
    jewel_rd,
    jewel_team,
    jewel_unassign_user,
    jewel_user,
    make_local,
    ok,
    pull,
    set_ansible_id,
    suffix,
    wait,
)

PASSWORD = "Drift-test-pw-1!"
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")


def as_user(username):
    return (username, PASSWORD)


def awx_pk(model, name, field="name"):
    return awx(f"from awx.main.models import {model}\nprint('@@' + json.dumps({model}.objects.get({field}={name!r}).pk))")


def awx_has(model, name, field="name"):
    return awx(f"from awx.main.models import {model}\nprint('@@' + json.dumps({model}.objects.filter({field}={name!r}).exists()))")


def world():
    s = suffix()
    w = SimpleNamespace(s=s, org=f"o-{s}", team=f"t-{s}", alice=f"alice-{s}", bob=f"bob-{s}", inv=f"inv-{s}", cred=f"cred-{s}", jt=f"jt-{s}")
    w.jewel_org = jewel_org(w.org)
    w.jewel_team = jewel_team(w.team, w.jewel_org)
    w.jewel_alice = jewel_user(w.alice, PASSWORD)
    w.jewel_bob = jewel_user(w.bob, PASSWORD)
    ok(jewel_assign_user("Team Member", w.jewel_alice, w.jewel_team))
    assert wait(lambda: awx_has("Team", w.team) and awx_has("User", w.bob, "username")), "Jewel's pushes did not reach AWX"
    w.awx_org = awx_pk("Organization", w.org)
    w.awx_team = awx_pk("Team", w.team)
    w.awx_inv = ok(awx_api("POST", "inventories/", json_body={"name": w.inv, "organization": w.awx_org}))["id"]
    w.awx_cred = ok(awx_api("POST", "credentials/", json_body={"name": w.cred, "organization": w.awx_org, "credential_type": 1}))["id"]
    project = ok(awx_api("GET", "projects/", params={"name": "Demo Project"}))["results"][0]["id"]
    w.awx_jt = ok(awx_api("POST", "job_templates/", json_body={"name": w.jt, "project": project, "playbook": "hello_world.yml", "inventory": w.awx_inv}))["id"]
    ok(jewel_assign_team("JobTemplate Execute", w.jewel_team, w.awx_jt))
    ok(jewel_assign_user("Inventory Use", w.jewel_bob, w.awx_inv))
    w.names = [w.org, w.team, w.alice, w.bob, w.inv, w.cred, w.jt, "admin"]
    w.state = lambda: awx_state(*w.names)
    return w


def jewel_snapshot(w):
    """What Jewel has for this world: its objects and assignments by name"""
    orgs = [o["name"] for o in ok(jewel("GET", "organizations/", params={"name__startswith": f"o-{w.s}"}))["results"]]
    teams = [t["name"] for t in ok(jewel("GET", "teams/", params={"name__startswith": f"t-{w.s}"}))["results"]]
    users = [u["username"] for u in ok(jewel("GET", "users/", params={"username__contains": w.s}))["results"]]
    roles = []
    for a in ok(jewel("GET", "role_user_assignments/", params={"user__username__contains": w.s, "page_size": 200}))["results"]:
        roles.append([a["summary_fields"]["user"]["username"], a["summary_fields"]["role_definition"]["name"], a.get("object_id")])
    for a in ok(jewel("GET", "role_team_assignments/", params={"team__name__contains": w.s, "page_size": 200}))["results"]:
        roles.append([a["summary_fields"]["team"]["name"], a["summary_fields"]["role_definition"]["name"], a.get("object_id")])
    return {"orgs": orgs, "teams": teams, "users": users, "roles": sorted(roles)}


SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ---- baseline ----


@scenario
def baseline(w):
    before = w.state()
    p = pull()
    return {"world": before, "pull": p, "after_pull": w.state(), "jewel": jewel_snapshot(w)}


# ---- pull ----


@scenario
def p1_corrupt_org(w):
    bad = corrupt("Organization", w.org)
    p = pull()
    return {"bad_id": bad, "pull": p, "after": w.state(), "awx_org_pk_before": w.awx_org}


@scenario
def p2_corrupt_team(w):
    corrupt("Team", w.team)
    p = pull()
    return {"pull": p, "after": w.state(), "awx_team_pk_before": w.awx_team}


@scenario
def p3_corrupt_user(w):
    before = w.state()
    corrupt("User", w.alice, "username")
    corrupt("User", w.bob, "username")
    p = pull()
    return {"before": before, "pull": p, "after": w.state()}


@scenario
def p4_local_unchanged_and_changed(w):
    make_local("Team", w.team)
    make_local("Organization", w.org)
    awx(
        f"from awx.main.models import Organization\nOrganization.objects.filter(name={w.org!r}).update(description='changed in AWX')\nprint('@@' + json.dumps(True))"
    )
    p = pull()
    desc = awx(f"from awx.main.models import Organization\nprint('@@' + json.dumps(Organization.objects.get(name={w.org!r}).description))")
    return {"pull": p, "after": w.state(), "org_description": desc}


@scenario
def p9_p8_awx_only_roles(w):
    awx(
        f"""
        from ansible_base.rbac.models import RoleDefinition
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import Team, User, JobTemplate
        with no_reverse_sync():
            RoleDefinition.objects.get(name='Team Admin').give_permission(User.objects.get(username={w.alice!r}), Team.objects.get(name={w.team!r}))
            RoleDefinition.objects.get(name='JobTemplate Admin').give_permission(User.objects.get(username={w.bob!r}), JobTemplate.objects.get(name={w.jt!r}))
        print('@@' + json.dumps(True))
        """
    )
    before = w.state()
    p = pull()
    return {"before": before, "pull": p, "after": w.state()}


@scenario
def p11_jewel_role_changes(w):
    out = {}
    for role, obj in (("Team Admin", w.jewel_team), ("Organization Member", w.jewel_org), ("Organization Admin", w.jewel_org)):
        out[f"add {role}"] = jewel_assign_user(role, w.jewel_bob, obj)[0]
    time.sleep(10)
    out["after_adds_push"] = w.state()
    for role, obj in (("Team Member", w.jewel_team),):
        out[f"remove {role}"] = [r[0] for r in jewel_unassign_user(role, w.jewel_alice, obj)]
    out["team_org_member"] = jewel_assign_team("Organization Member", w.jewel_team, w.jewel_org)
    out["team_org_inventory_admin"] = jewel_assign_team("Organization Inventory Admin", w.jewel_team, w.awx_org)
    time.sleep(10)
    out["after_push"] = w.state()
    out["pull"] = pull()
    out["after_pull"] = w.state()
    out["jewel"] = jewel_snapshot(w)
    return out


@scenario
def p10_role_definitions(w):
    out = {}
    tm = ok(jewel("GET", "role_definitions/", params={"name": "Team Member"}))["results"][0]
    out["rename_managed_team_member"] = jewel("PATCH", f"role_definitions/{tm['id']}/", json_body={"name": "Team Participant"})
    ta = ok(jewel("GET", "role_definitions/", params={"name": "Team Admin"}))["results"][0]
    out["delete_managed_team_admin"] = jewel("DELETE", f"role_definitions/{ta['id']}/")[0]
    # an old-RBAC write with no managed equivalent makes a Compat role definition
    roles = ok(awx_api("GET", f"job_templates/{w.awx_jt}/object_roles/"))["results"]
    out["jt_object_roles"] = [r["name"] for r in roles]
    rid = next((r["id"] for r in roles if r["name"] == "Read"), None)
    bob_pk = awx_pk("User", w.bob, "username")
    out["old_api_grant_read"] = awx_api("POST", f"users/{bob_pk}/roles/", json_body={"id": rid})[0] if rid else None
    time.sleep(5)
    out["compat_rds_awx"] = awx(
        "from ansible_base.rbac.models import RoleDefinition\nfrom ansible_base.resource_registry.models import Resource, service_id\n"
        "print('@@' + json.dumps([[rd.name, 'local' if str(Resource.get_resource_for_object(rd).service_id) == service_id() else 'jewel'] for rd in RoleDefinition.objects.filter(name__endswith='Compat')]))"
    )
    out["compat_rds_jewel"] = [r["name"] for r in ok(jewel("GET", "role_definitions/", params={"name__endswith": "Compat"}))["results"]]
    out["state"] = w.state()
    return out


# ---- push on save (AWX API, through Jewel) ----


@scenario
def w_team_saves(w):
    out = {}
    out["create_team_in_awx"] = awx_api("POST", "teams/", json_body={"name": f"t9-{w.s}", "organization": w.awx_org})
    time.sleep(3)
    out["jewel_has_t9"] = ok(jewel("GET", "teams/", params={"name": f"t9-{w.s}"}))["count"]
    out["update_team_in_awx"] = awx_api("PATCH", f"teams/{w.awx_team}/", json_body={"description": "changed in AWX"})
    time.sleep(3)
    out["jewel_team_description"] = ok(jewel("GET", f"teams/{w.jewel_team}/"))["description"]
    return out


@scenario
def w3_w6_corrupt_team(w):
    corrupt("Team", w.team)
    out = {"update": awx_api("PATCH", f"teams/{w.awx_team}/", json_body={"description": "x"}), "delete": awx_api("DELETE", f"teams/{w.awx_team}/")}
    out["after"] = w.state()
    out["jewel"] = jewel_snapshot(w)
    return out


@scenario
def w4_local_team(w):
    make_local("Team", w.team)
    out = {"update": awx_api("PATCH", f"teams/{w.awx_team}/", json_body={"description": "changed in AWX"})[0]}
    out["jewel_description"] = ok(jewel("GET", f"teams/{w.jewel_team}/"))["description"]
    out["pull"] = pull()
    out["awx_description_after_pull"] = awx(f"from awx.main.models import Team\nprint('@@' + json.dumps(Team.objects.get(name={w.team!r}).description))")
    out["delete"] = awx_api("DELETE", f"teams/{w.awx_team}/")[0]
    time.sleep(3)
    out["jewel_has_team"] = ok(jewel("GET", "teams/", params={"name": w.team}))["count"]
    return out


@scenario
def w5_name_jewel_has(w):
    t2 = f"t2-{w.s}"
    jewel_team(t2, w.jewel_org)
    wait(lambda: awx_has("Team", t2))
    awx(
        f"""
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import Team
        with no_reverse_sync():
            Team.objects.filter(name={t2!r}).delete()
        print('@@' + json.dumps(True))
        """
    )
    out = {
        "create": awx_api("POST", "teams/", json_body={"name": t2, "organization": w.awx_org}),
        "rename": awx_api("PATCH", f"teams/{w.awx_team}/", json_body={"name": t2}),
    }
    out["jewel_t2_count"] = ok(jewel("GET", "teams/", params={"name": t2}))["count"]
    out["awx_has_t2"] = awx_has("Team", t2)
    return out


@scenario
def w6_delete_team_holding_other_id(w):
    t2 = f"t2-{w.s}"
    t2_gw = jewel_team(t2, w.jewel_org)
    wait(lambda: awx_has("Team", t2))
    t2_aid = jewel_ansible_id("teams", t2_gw)
    # AWX's copy of t2 gets out of the way, and t1 takes t2's ansible_id
    awx(
        f"""
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import Team
        with no_reverse_sync():
            Team.objects.filter(name={t2!r}).delete()
        print('@@' + json.dumps(True))
        """
    )
    set_ansible_id("Team", w.team, t2_aid)
    out = {"delete_t1_in_awx": awx_api("DELETE", f"teams/{w.awx_team}/")}
    time.sleep(3)
    out["jewel_t1"] = ok(jewel("GET", "teams/", params={"name": w.team}))["count"]
    out["jewel_t2"] = ok(jewel("GET", "teams/", params={"name": t2}))["count"]
    return out


# ---- assignments ----


@scenario
def r1_r2_corrupt_user(w):
    corrupt("User", w.bob, "username")
    bob = awx_pk("User", w.bob, "username")
    rd = ok(awx_api("GET", "role_definitions/", params={"name": "Team Member"}))["results"][0]["id"]
    out = {
        "new_api": awx_api("POST", "role_user_assignments/", json_body={"role_definition": rd, "user": bob, "object_id": w.awx_team}),
        "old_api": awx_api("POST", f"teams/{w.awx_team}/users/", json_body={"id": bob}),
    }
    out["after"] = w.state()
    out["jewel"] = jewel_snapshot(w)
    return out


# ---- JWT login ----


@scenario
def j1_corrupt_user_login(w):
    corrupt("User", w.alice, "username")
    out = {"me": awx_api("GET", "me/", auth=as_user(w.alice))[0], "after": w.state(), "alice_pk_before": awx_pk("User", w.alice, "username")}
    return out


@scenario
def j3_rename(w):
    out = {"rename": jewel("PATCH", f"users/{w.jewel_alice}/", json_body={"username": f"alicia-{w.s}"})[0]}
    time.sleep(8)
    out["awx_after_push"] = awx_state(f"alicia-{w.s}", w.alice)
    out["me"] = awx_api("GET", "me/", auth=(f"alicia-{w.s}", PASSWORD))[0]
    out["awx_after_login"] = awx_state(f"alicia-{w.s}", w.alice)
    return out


@scenario
def j3_rename_clash(w):
    taken = f"taken-{w.s}"
    awx(
        f"""
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import User
        with no_reverse_sync():
            User.objects.create(username={taken!r})
        print('@@' + json.dumps(True))
        """
    )
    out = {"rename": jewel("PATCH", f"users/{w.jewel_alice}/", json_body={"username": taken})[0]}
    time.sleep(8)
    out["awx_after_push"] = awx_state(taken, w.alice)
    out["me"] = awx_api("GET", "me/", auth=(taken, PASSWORD))
    out["awx_after_login"] = awx_state(taken, w.alice)
    return out


@scenario
def j4_reused_username(w):
    bob_pk = awx_pk("User", w.bob, "username")
    out = {"delete_bob": jewel("DELETE", f"users/{w.jewel_bob}/")[0]}
    time.sleep(8)
    out["awx_has_old_bob"] = awx(f"from awx.main.models import User\nprint('@@' + json.dumps(User.objects.filter(pk={bob_pk}).exists()))")
    jewel_user(w.bob, PASSWORD, first_name="Someone", last_name="Else")
    time.sleep(8)
    out["me"] = awx_api("GET", "me/", auth=as_user(w.bob))
    out["inventory"] = awx_api("GET", f"inventories/{w.awx_inv}/", auth=as_user(w.bob))[0]
    out["bob_pk_now"] = awx_pk("User", w.bob, "username")
    out["old_bob_pk"] = bob_pk
    out["after"] = w.state()
    return out


@scenario
def j5_j7_claims(w):
    o2, t2 = f"o2-{w.s}", f"t2-{w.s}"
    o2_gw = jewel_org(o2)
    t2_gw = jewel_team(t2, o2_gw)
    ok(jewel_assign_user("Team Member", w.jewel_alice, t2_gw))
    time.sleep(8)
    out = {"after_push": awx_state(o2, t2, w.alice)}
    jewel_unassign_user("Team Member", w.jewel_alice, w.jewel_team)
    time.sleep(8)
    out["after_unassign_push"] = w.state()
    out["me"] = awx_api("GET", "me/", auth=as_user(w.alice))[0]
    out["after_login"] = awx_state(o2, t2, w.alice, w.team)
    return out


@scenario
def j6_corrupt_team(w):
    bad = corrupt("Team", w.team)
    out = {"alice_me": awx_api("GET", "me/", auth=as_user(w.alice))[0]}
    out["team_after_member_login"] = awx_state(w.team)["teams"]
    out["add_bob_in_jewel"] = jewel_assign_user("Team Member", w.jewel_bob, w.jewel_team)[0]
    time.sleep(8)
    out["after_push"] = w.state()
    out["bob_me"] = awx_api("GET", "me/", auth=as_user(w.bob))[0]
    out["after_bob_login"] = w.state()
    out["bad_id"] = bad
    return out


@scenario
def j9_superuser(w):
    out = {"on": jewel("PATCH", f"users/{w.jewel_alice}/", json_body={"is_superuser": True})[0]}
    time.sleep(8)
    out["awx_after_push_on"] = awx_state(w.alice)["users"]
    out["me_on"] = ok(awx_api("GET", "me/", auth=as_user(w.alice)))["results"][0]["is_superuser"]
    out["off"] = jewel("PATCH", f"users/{w.jewel_alice}/", json_body={"is_superuser": False})[0]
    time.sleep(8)
    out["awx_after_push_off"] = awx_state(w.alice)["users"]
    out["me_off"] = ok(awx_api("GET", "me/", auth=as_user(w.alice)))["results"][0]["is_superuser"]
    return out


# ---- Jewel's pushes (the E endpoints) ----


@scenario
def e1_name_awx_has(w):
    o3 = f"o3-{w.s}"
    awx(
        f"""
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import Organization
        with no_reverse_sync():
            Organization.objects.create(name={o3!r})
        print('@@' + json.dumps(True))
        """
    )
    before = awx_state(o3)
    out = {"create_in_jewel": jewel("POST", "organizations/", json_body={"name": o3})[0]}
    time.sleep(8)
    out["awx_before"] = before
    out["awx_after_push"] = awx_state(o3)
    out["pull"] = pull()
    out["awx_after_pull"] = awx_state(o3)
    return out


@scenario
def e2_assign_unknown_user(w):
    corrupt("User", w.bob, "username")
    out = {"jewel_assign": jewel_assign_user("Team Member", w.jewel_bob, w.jewel_team)[0]}
    time.sleep(8)
    out["after_push"] = w.state()
    out["jewel"] = jewel_snapshot(w)
    out["pull"] = pull()
    out["after_pull"] = w.state()
    return out


@scenario
def e3_delete_org(w):
    out = {"delete_org_in_jewel": jewel("DELETE", f"organizations/{w.jewel_org}/")[0]}
    time.sleep(10)
    out["after"] = w.state()
    return out


def _team_aids(name):
    """AWX's and Jewel's ansible_id for a team name, and how many Jewel has"""
    awx_aid = awx(
        f"from awx.main.models import Team\nfrom ansible_base.resource_registry.models import Resource\n"
        f"print('@@' + json.dumps([str(Resource.get_resource_for_object(t).ansible_id) for t in Team.objects.filter(name={name!r})]))"
    )
    teams = ok(jewel("GET", "teams/", params={"name": name}))["results"]
    return {"awx": awx_aid, "jewel": [t["summary_fields"]["resource"]["ansible_id"] for t in teams]}


def _w5_setup(w):
    """Jewel has t2 and AWX does not, as if Jewel's create push never arrived"""
    t2 = f"t2-{w.s}"
    jewel_team(t2, w.jewel_org)
    wait(lambda: awx_has("Team", t2))
    awx(
        f"""
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import Team
        with no_reverse_sync():
            Team.objects.filter(name={t2!r}).delete()
        print('@@' + json.dumps(True))
        """
    )
    return t2


@scenario
def w5_create(w):
    t2 = _w5_setup(w)
    out = {"before": _team_aids(t2)}
    out["create"] = awx_api("POST", "teams/", json_body={"name": t2, "organization": w.awx_org})
    out["create"] = [out["create"][0], out["create"][1] if out["create"][0] >= 400 else "created"]
    out["after"] = _team_aids(t2)
    out["pull"] = pull()
    out["after_pull"] = _team_aids(t2)
    return out


@scenario
def w5_rename(w):
    t2 = _w5_setup(w)
    out = {"before": _team_aids(t2), "t1_before": _team_aids(w.team)}
    out["rename"] = awx_api("PATCH", f"teams/{w.awx_team}/", json_body={"name": t2})
    out["rename"] = [out["rename"][0], out["rename"][1] if out["rename"][0] >= 400 else "renamed"]
    out["after"] = _team_aids(t2)
    out["t1_after"] = _team_aids(w.team)
    out["pull"] = pull()
    out["after_pull"] = _team_aids(t2)
    out["t1_after_pull"] = _team_aids(w.team)
    return out


@scenario
def p11_team_org_inventory_admin(w):
    out = {"assign": jewel_assign_team("Organization Inventory Admin", w.jewel_team, w.jewel_org)[0]}
    time.sleep(10)
    out["after_push"] = w.state()
    assignment = ok(jewel("GET", "role_team_assignments/", params={"team": w.jewel_team, "role_definition": jewel_rd("Organization Inventory Admin")}))[
        "results"
    ]
    out["unassign"] = [jewel("DELETE", f"role_team_assignments/{a['id']}/")[0] for a in assignment]
    time.sleep(10)
    out["after_unassign_push"] = w.state()
    return out


@scenario
def p10_custom_role_definition(w):
    name, renamed = f"Inventory Viewer {w.s}", f"Org Inventory Viewer {w.s}"
    out = {}
    created = jewel(
        "POST",
        "role_definitions/",
        json_body={"name": name, "description": "", "permissions": ["awx.view_inventory", "shared.view_organization"], "content_type": "shared.organization"},
    )
    out["create"] = created
    if created[0] != 201:
        return out
    rd = created[1]["id"]
    out["assign"] = jewel("POST", "role_user_assignments/", json_body={"role_definition": rd, "user": w.jewel_alice, "object_id": w.jewel_org})[0]
    time.sleep(10)
    rd_state = (
        "from ansible_base.rbac.models import RoleDefinition, RoleUserAssignment\nfrom ansible_base.resource_registry.models import Resource, service_id\n"
        "names = {names!r}\n"
        "rds = [[rd.name, 'local' if str(Resource.get_resource_for_object(rd).service_id) == service_id() else 'jewel', sorted(p.api_slug for p in rd.permissions.all())] for rd in RoleDefinition.objects.filter(name__in=names)]\n"
        "assignments = sorted([a.user.username, a.role_definition.name] for a in RoleUserAssignment.objects.filter(role_definition__name__in=names))\n"
        "print('@@' + json.dumps({{'role_definitions': rds, 'assignments': assignments}}))"
    )
    names = [name, renamed]
    out["after_create"] = awx(rd_state.format(names=names))
    out["alice_inventory"] = awx_api("GET", f"inventories/{w.awx_inv}/", auth=as_user(w.alice))[0]
    out["rename"] = jewel("PATCH", f"role_definitions/{rd}/", json_body={"name": renamed})[0]
    time.sleep(10)
    out["after_rename"] = awx(rd_state.format(names=names))
    out["delete"] = jewel("DELETE", f"role_definitions/{rd}/")[0]
    time.sleep(10)
    out["after_delete"] = awx(rd_state.format(names=names))
    return out


def main(names):
    results = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    for name in names or SCENARIOS:
        print(f"--- {name}", flush=True)
        try:
            w = world()
            results[name] = {"ok": True, "suffix": w.s, "result": SCENARIOS[name](w)}
        except Exception:
            results[name] = {"ok": False, "error": traceback.format_exc()[-2000:]}
        print(json.dumps(results[name], default=str)[:1500], flush=True)
        json.dump(results, open(RESULTS, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1:])
