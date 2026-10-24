"""
The dab_sync rows that need lost pushes, out-of-order pushes or Jewel failures, run against a
real AAP with faults injected (see faults.py). Each scenario records what happened and a
`checks` dict that mirrors the assertions of the AWX test named in its docstring.

    uv run --no-project --with requests python fault_scenarios.py [scenario ...]

With no names it runs every scenario here except f_p6_empty_team_manifest, which deletes every
team AWX has from Jewel (a normal pull restores them); name it to run it.
"""

import sys

from faults import awx_faulty, jewel_api, jewel_deliver, jewel_jwt
from live import awx, awx_api, jewel, jewel_rd, ok, pull, wait
from scenarios import PASSWORD, as_user, awx_pk, corrupt, main, scenario, world

TEAM_MANIFEST = "*/resource-types/shared.team/manifest/"
UNREACHABLE = {"exc": "ConnectionError"}
DESTRUCTIVE = {"f_p6_empty_team_manifest"}


def has_role(state, actor, role, obj):
    """An assignment in awx_state() output, for a user or a team, on an object named obj"""
    return any(a == actor and r == role and _names(o, obj) for a, r, o in state["user_roles"] + state["team_roles"])


def _names(text, name):
    """AWX's str() of an object is its name, with "-<pk>" appended for some models"""
    return text is not None and (text == name or text.startswith(name + "-") and text[len(name) + 1 :].isdigit())


def has_old_role(state, user, role_field, obj):
    return any(r == role_field and u == user and _names(o, obj) for _, o, r, u in state["old_members"])


def jewel_user_assignment(role, user, object_id):
    """Jewel's id for a user's role assignment"""
    [a] = ok(jewel("GET", "role_user_assignments/", params={"role_definition": jewel_rd(role), "user": user, "object_id": object_id}))["results"]
    return a["id"]


def unassign_push_lost(role, user, object_id):
    """Jewel removes a user's role and its push never reaches AWX"""
    return jewel_api(("DELETE", f"role_user_assignments/{jewel_user_assignment(role, user, object_id)}/", None))["responses"][0][0]


def assign_push_lost(role, user, object_id):
    return jewel_api(("POST", "role_user_assignments/", {"role_definition": jewel_rd(role), "user": user, "object_id": object_id}))["responses"][0][0]


def jewel_team_aid(w):
    return ok(jewel("GET", f"teams/{w.jewel_team}/"))["summary_fields"]["resource"]["ansible_id"]


def give_bob_jt_admin_awx_only(w):
    awx(
        f"""
        from ansible_base.rbac.models import RoleDefinition
        from ansible_base.resource_registry.signals.handlers import no_reverse_sync
        from awx.main.models import JobTemplate, User
        with no_reverse_sync():
            RoleDefinition.objects.get(name='JobTemplate Admin').give_permission(User.objects.get(username={w.bob!r}), JobTemplate.objects.get(name={w.jt!r}))
        print('@@' + json.dumps(True))
        """
    )


def faulty_pull(*faults):
    return awx_faulty("result = pull()", faults)


def jewel_team_count():
    return awx(
        "from awx.main.models import Team\nfrom ansible_base.resource_registry.models import Resource, service_id\n"
        "print('@@' + json.dumps(sum(1 for t in Team.objects.all() if str(Resource.get_resource_for_object(t).service_id) != service_id())))"
    )


# ---- P6: unusable manifests ----


@scenario
def f_p6_team_manifest_404(w):
    """test_pull_skips_type_without_manifest"""
    run = faulty_pull({"method": "GET", "path": TEAM_MANIFEST, "status": 404, "body": {"detail": "Not found."}})
    after = w.state()
    checks = {
        "team_kept": w.team in after["teams"],
        "alice_member": has_role(after, w.alice, "Team Member", w.team),
        "team_can_execute": has_role(after, w.team, "JobTemplate Execute", w.jt),
    }
    return {"pull": run["result"], "checks": checks}


@scenario
def f_p6_empty_team_manifest(w):
    """test_pull_deletes_every_team_for_empty_manifest"""
    before = jewel_team_count()
    empty = {
        "method": "GET",
        "path": TEAM_MANIFEST,
        "status": 200,
        "body": "ansible_id,resource_hash\n",
        "headers": {"X-Resource-Count": "0", "Content-Type": "text/csv"},
    }
    run = faulty_pull(empty)
    after = w.state()
    deleted = jewel_team_count()
    restore = pull()
    checks = {
        "every_jewel_team_deleted": deleted == 0,
        "alice_not_member": not has_role(after, w.alice, "Team Member", w.team),
        "team_roles_gone": not after["team_roles"],
    }
    return {"teams_before": before, "teams_after": deleted, "pull": run["result"], "restore": restore, "teams_restored": jewel_team_count(), "checks": checks}


@scenario
def f_p6_unusable_manifest_corrupted_team(w):
    """test_pull_with_unusable_manifest_and_corrupted_team, each parametrization on its own world"""
    manifests = {
        "not_found": {"status": 404, "body": {"detail": "Not found."}},
        "no_count_header": {"drop_headers": ["X-Resource-Count"]},
        "count_mismatch": {"headers": {"X-Resource-Count": "999999"}},
    }
    out = {}
    for name, manifest in manifests.items():
        if out:
            w = world()
        bad = corrupt("Team", w.team)
        run = faulty_pull({"method": "GET", "path": TEAM_MANIFEST, **manifest})
        after = w.state()
        aid = awx(
            f"from awx.main.models import Team\nprint('@@' + json.dumps([[t.pk, str(t.resource.ansible_id)] for t in Team.objects.filter(name={w.team!r})]))"
        )
        recreated = "X-Resource-Count" in manifest.get("headers", {})
        [[pk, ansible_id]] = aid
        if recreated:
            checks = {"team_recreated": pk != w.awx_team and ansible_id == jewel_team_aid(w)}
        else:
            checks = {"team_kept_with_bad_id": pk == w.awx_team and ansible_id == bad}
        checks["alice_member" if recreated else "alice_not_member"] = has_role(after, w.alice, "Team Member", w.team) == recreated
        checks["team_can_execute" if recreated else "team_cannot_execute"] = has_role(after, w.team, "JobTemplate Execute", w.jt) == recreated
        out[name] = {"pull": run["result"], "manifest_call": [c for c in run["calls"] if "shared.team/manifest" in c[1]], "checks": checks}
        pull()
    return out


# ---- P7: Jewel fails during the pull ----


@scenario
def f_p7_pull_stops(w):
    """test_pull_stops_when_jewel_fails"""
    failures = {
        "metadata_500": {"path": "*/metadata/", "status": 500, "body": "Internal Server Error"},
        "metadata_unreachable": {"path": "*/metadata/", **UNREACHABLE},
        "team_manifest_500": {"path": TEAM_MANIFEST, "status": 500, "body": "Internal Server Error"},
    }
    out = {"unassign_push_lost": unassign_push_lost("Team Member", w.jewel_alice, w.jewel_team)}
    for name, failure in failures.items():
        run = faulty_pull({"method": "GET", **failure})
        after = w.state()
        checks = {
            "raised": run["result"]["raised"] is not None,
            "alice_still_member": has_role(after, w.alice, "Team Member", w.team),
            "no_assignment_requests": not [c for c in run["calls"] if "assignments" in c[1]],
        }
        out[name] = {"pull": run["result"], "checks": checks}
    out["clean_pull"] = pull()
    out["alice_member_after_clean_pull"] = has_role(w.state(), w.alice, "Team Member", w.team)
    return out


@scenario
def f_p7_fetch_team_fails(w):
    """test_pull_fails_fetching_team_after_deleting_it, each failure on its own world"""
    failures = {
        "jewel_500": {"status": 500, "body": "Internal Server Error"},
        "jewel_404": {"status": 404, "body": {"detail": "Not found."}},
        "unreachable": UNREACHABLE,
    }
    out = {}
    for name, failure in failures.items():
        if out:
            w = world()
        corrupt("Team", w.team)
        run = faulty_pull({"method": "GET", "path": f"*/service-index/resources/{jewel_team_aid(w)}/", **failure})
        after = w.state()
        clean = pull()
        restored = w.state()
        checks = {
            "raised" if name != "jewel_404" else "did_not_raise": (run["result"]["raised"] is not None) == (name != "jewel_404"),
            "no_team_after_pull": w.team not in after["teams"],
            "no_team_member_roles": not has_role(after, w.alice, "Team Member", w.team),
            "next_pull_recreates_team": w.team in restored["teams"],
            "next_pull_restores_member": has_role(restored, w.alice, "Team Member", w.team),
            "next_pull_restores_execute": has_role(restored, w.team, "JobTemplate Execute", w.jt),
        }
        out[name] = {"pull": run["result"], "clean_pull": clean, "checks": checks}
    return out


# ---- P8: an assignment list fails ----


@scenario
def f_p8_assignment_list_fails(w):
    """test_pull_keeps_assignments_when_assignment_list_fails"""
    failures = {
        "user_list_500": {"path": "*/role-user-assignments/", "status": 500, "body": "Internal Server Error"},
        "user_list_unreachable": {"path": "*/role-user-assignments/", **UNREACHABLE},
        "team_list_500": {"path": "*/role-team-assignments/", "status": 500, "body": "Internal Server Error"},
    }
    give_bob_jt_admin_awx_only(w)
    out = {}
    for name, failure in failures.items():
        run = faulty_pull({"method": "GET", **failure})
        after = w.state()
        checks = {
            "did_not_raise": run["result"]["raised"] is None,
            "bob_keeps_jt_admin": has_role(after, w.bob, "JobTemplate Admin", w.jt),
            "alice_still_member": has_role(after, w.alice, "Team Member", w.team),
        }
        out[name] = {"pull": run["result"], "failed_calls": [c for c in run["calls"] if "injected" in c or "ConnectionError" in c], "checks": checks}
    out["clean_pull"] = pull()
    out["clean_pull_removes_jt_admin"] = not has_role(w.state(), w.bob, "JobTemplate Admin", w.jt)
    return out


# ---- W1/W2: Jewel fails a push on save ----


@scenario
def f_w1_w2_jewel_fails_team_change(w):
    """test_jewel_failure_rejects_team_change, through AWX's API in-process"""
    failures = {
        "jewel_400": {"status": 400, "body": {"detail": "rejected by Jewel"}},
        "jewel_503": {"status": 503, "body": "Service Unavailable"},
        "unreachable": UNREACHABLE,
    }
    actions = {
        "create": ("POST", "POST", "/api/controller/v2/teams/", {"name": f"t9-{w.s}", "organization": w.awx_org}),
        "update": ("PATCH", "PUT", f"/api/controller/v2/teams/{w.awx_team}/", {"description": "changed in AWX"}),
        "delete": ("DELETE", "DELETE", f"/api/controller/v2/teams/{w.awx_team}/", None),
    }
    out = {}
    for action, (method, push_method, path, body) in actions.items():
        for name, failure in failures.items():
            run = awx_faulty(
                f"""
from awx.main.models import Team
response = api_call({method!r}, {path!r}, {body!r}, user="admin")
result = {{"response": response, "teams": sorted([t.name, t.description] for t in Team.objects.filter(name__in=[{w.team!r}, "t9-{w.s}"]))}}
""",
                [{"method": push_method, "path": "*/service-index/resources/*", **failure}],
            )
            pushes = [c for c in run["calls"] if "/service-index/resources/" in c[1]]
            checks = {
                "status_400": run["result"]["response"][0] == 400,
                "generic_message": run["result"]["response"][1] == ["Failed to sync resource to resource server"],
                "one_push_of_method": [c[0] for c in pushes] == [push_method],
                "rolled_back": run["result"]["teams"] == [[w.team, ""]],
            }
            out[f"{action}_{name}"] = {"response": run["result"]["response"], "pushes": pushes, "checks": checks}
    return out


# ---- J4: a reused username with lost or out-of-order pushes ----


def _replace_bob_pushes_held(w):
    """Jewel deletes bob and someone new signs up as bob, with both pushes held"""
    run = jewel_api(
        ("DELETE", f"users/{w.jewel_bob}/", None),
        ("POST", "users/", {"username": w.bob, "password": PASSWORD, "first_name": "Someone", "last_name": "Else"}),
    )
    return run, run["responses"][1][1]["summary_fields"]["resource"]["ansible_id"]


def _bob_now(w):
    return awx(
        f"from awx.main.models import User\nprint('@@' + json.dumps([[u.pk, str(u.resource.ansible_id)] for u in User.objects.filter(username={w.bob!r})]))"
    )


@scenario
def f_j4_pushes_lost(w):
    """test_reused_username_takes_over_previous_user_when_pushes_are_lost"""
    old_bob = awx_pk("User", w.bob, "username")
    run, new_bob = _replace_bob_pushes_held(w)
    me = awx_api("GET", "me/", auth=as_user(w.bob))
    inventory = awx_api("GET", f"inventories/{w.awx_inv}/", auth=as_user(w.bob))[0]
    after = w.state()
    checks = {
        "login_200": me[0] == 200,
        "logged_in_as_old_bob": me[0] == 200 and me[1]["results"][0]["id"] == old_bob,
        "old_bob_has_new_ansible_id": _bob_now(w) == [[old_bob, new_bob]],
        "inventory_200": inventory == 200,
        "keeps_inventory_use": has_role(after, w.bob, "Inventory Use", w.inv),
    }
    return {"jewel": [r[0] for r in run["responses"]], "held": [[p["method"], p["path"]] for p in run["held"]], "inventory": inventory, "checks": checks}


@scenario
def f_j4_pushes_out_of_order(w):
    """test_reused_username_gets_a_new_user_when_pushes_arrive_out_of_order"""
    old_bob = awx_pk("User", w.bob, "username")
    run, new_bob = _replace_bob_pushes_held(w)
    creates = [p for p in run["held"] if p["method"].lower() == "post"]
    others = [p for p in run["held"] if p["method"].lower() != "post"]
    answers = jewel_deliver(creates + others)
    bob_before_login = _bob_now(w)
    me = awx_api("GET", "me/", auth=as_user(w.bob))
    inventory = awx_api("GET", f"inventories/{w.awx_inv}/", auth=as_user(w.bob))[0]
    bob = _bob_now(w)
    create = next(a for a in answers if a[0].lower() == "post")
    delete = next(a for a in answers if a[0].lower() == "delete")
    checks = {
        "create_400": create[2] == 400,
        "create_unique_error": "UNIQUE" in str(create[3]).upper(),
        "delete_204": delete[2] == 204,
        "no_bob_before_login": bob_before_login == [],
        "login_200": me[0] == 200,
        "new_user_with_new_ansible_id": len(bob) == 1 and bob[0][0] != old_bob and bob[0][1] == new_bob,
        "inventory_403": inventory == 403,
        "inventory_use_gone": not has_role(w.state(), w.bob, "Inventory Use", w.inv),
    }
    return {"answers": answers, "checks": checks}


# ---- J8: the jwt_claims fetch fails at login ----


def _login(jwt, faults):
    run = awx_faulty('result = api_call("GET", "/api/controller/v2/me/", jwt=JWT)'.replace("JWT", repr(jwt)), faults)
    return run["result"], [c for c in run["calls"] if "/jwt_claims/" in c[1]]


@scenario
def f_j8_claims_fetch_fails(w):
    """test_login_fails_when_claims_fetch_fails"""
    failures = {
        "jewel_404": {"status": 404, "body": {"error": "User not found"}},
        "jewel_500": {"status": 500, "body": "Internal Server Error"},
        "unreachable": UNREACHABLE,
    }
    out = {"unassign_push_lost": unassign_push_lost("Team Member", w.jewel_alice, w.jewel_team)}
    jwt = jewel_jwt(w.alice)
    for name, failure in failures.items():
        response, fetches = _login(jwt, [{"method": "GET", "path": "*/jwt_claims/*", **failure}])
        checks = {
            "status_401": response[0] == 401,
            "one_claims_fetch": [c[0] for c in fetches] == ["GET"],
            "alice_still_member": has_role(w.state(), w.alice, "Team Member", w.team),
        }
        out[name] = {"response": response, "fetches": fetches, "checks": checks}
    out["clean_pull"] = pull()
    return out


@scenario
def f_j8_jewel_locked(w):
    """test_login_while_jewel_is_locked, for alice and for admin"""
    out = {}
    for username, jewel_user_id, status in (
        (w.alice, w.jewel_alice, 401),
        ("admin", jewel("GET", "users/", params={"username": "admin"})[1]["results"][0]["id"], 200),
    ):
        added = assign_push_lost("Team Admin", jewel_user_id, w.jewel_team)
        response, fetches = _login(jewel_jwt(username), [{"method": "GET", "path": "*/jwt_claims/*", "status": 423, "body": {"detail": "Locked"}}])
        checks = {
            f"status_{status}": response[0] == status,
            "fetch_got_423": [c[2] for c in fetches] == [423],
            "no_team_admin": not has_role(w.state(), username, "Team Admin", w.team),
        }
        out[username] = {"jewel_assign": added, "response": response if response[0] != 200 else 200, "fetches": fetches, "checks": checks}
        unassign_push_lost("Team Admin", jewel_user_id, w.jewel_team)
    out["clean_pull"] = pull()
    return out


# ---- E3 and P10: Jewel's deletes ----


def _delete_in_jewel(path):
    """Jewel deletes through its API with the pushes held, and AWX's answers to them"""
    run = jewel_api(("DELETE", path, None))
    return run["responses"][0][0], jewel_deliver(run["held"])


@scenario
def f_e3_jewel_deletes_team_and_user(w):
    """test_jewel_deletes_team_or_user"""
    team = _delete_in_jewel(f"teams/{w.jewel_team}/")
    user = _delete_in_jewel(f"users/{w.jewel_bob}/")
    after = w.state()
    checks = {
        "team_deleted": w.team not in after["teams"],
        "team_roles_gone": not [r for r in after["team_roles"] if r[0] == w.team],
        "alice_membership_gone": not has_role(after, w.alice, "Team Member", w.team),
        "user_deleted": w.bob not in after["users"],
        "user_roles_gone": not [r for r in after["user_roles"] if r[0] == w.bob],
    }
    return {"team": team, "user": user, "checks": checks}


@scenario
def f_e3_jewel_deletes_corrupted_team(w):
    """test_jewel_deletes_team_awx_has_under_another_ansible_id"""
    corrupt("Team", w.team)
    deleted = _delete_in_jewel(f"teams/{w.jewel_team}/")
    after_push = w.state()
    p = pull()
    after_pull = w.state()
    checks = {
        "push_404": [a[2] for a in deleted[1]] == [404],
        "team_kept": w.team in after_push["teams"],
        "next_pull_deletes_team": w.team not in after_pull["teams"],
    }
    return {"jewel": deleted, "pull": p, "checks": checks}


@scenario
def f_p10_jewel_deletes_compat_role_definition(w):
    """test_jewel_deletes_compat_role_definition"""
    roles = ok(awx_api("GET", f"job_templates/{w.awx_jt}/object_roles/"))["results"]
    read = next(r["id"] for r in roles if r["name"] == "Read")
    bob = awx_pk("User", w.bob, "username")
    grant = awx_api("POST", f"users/{bob}/roles/", json_body={"id": read})[0]
    wait(lambda: ok(jewel("GET", "role_definitions/", params={"name": "JobTemplate Read Compat"}))["count"], timeout=30)
    before = w.state()
    [compat] = ok(jewel("GET", "role_definitions/", params={"name": "JobTemplate Read Compat"}))["results"]
    deleted = _delete_in_jewel(f"role_definitions/{compat['id']}/")
    after = w.state()
    rd_left = awx(
        "from ansible_base.rbac.models import RoleDefinition\nprint('@@' + json.dumps(RoleDefinition.objects.filter(name='JobTemplate Read Compat').exists()))"
    )
    checks = {
        "had_compat_assignment": has_role(before, w.bob, "JobTemplate Read Compat", w.jt),
        "had_read_role": has_old_role(before, w.bob, "read_role", w.jt),
        "role_definition_gone": not rd_left,
        "assignment_gone": not has_role(after, w.bob, "JobTemplate Read Compat", w.jt),
        "read_role_membership_stays": has_old_role(after, w.bob, "read_role", w.jt),
    }
    return {"grant": grant, "jewel": deleted, "checks": checks}


if __name__ == "__main__":
    fault_names = [name for name in globals() if name.startswith("f_") and name not in DESTRUCTIVE]
    main(sys.argv[1:] or fault_names)
