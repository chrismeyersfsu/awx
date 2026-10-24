# dab_sync live harness

Runs the cases from `awx/main/tests/functional/dab_sync/` against a real AAP install (Jewel,
AWX and the other services), so the fake Jewel in those tests can be checked against the real one.

Each scenario builds its own world through Jewel's API: an org, a team, two users, and an
inventory, credential and job template in AWX. It then makes the case's change and records what
AWX and Jewel end up with in `results.json`.

- `scenarios.py`: the cases that need only Jewel's API and drift injected into AWX's
  database (with `awx-manage shell` over ssh).
- `fault_scenarios.py`: the cases that need lost pushes, out-of-order pushes or Jewel
  failures. Each one records a `checks` dict that mirrors the assertions of the test named in
  its docstring.
- `faults.py`: how faults are injected (see its docstring). They go in at the same points the
  fake uses, inside one shell process on the instance, so the running services are untouched.
- `checks.py`: prints every fault scenario's checks from `results.json`.
- `show.py`: prints one scenario's result compactly.

## Setup

An AAP containerized all-in-one install you can ssh to, as the user that runs its containers.
Put its details in `instance.json`, mode 0600 (it is git-ignored):

```json
{
 "jewel_url": "https://<aap host>/",
 "admin_password": "<admin password>",
 "aio_host": "<host to ssh to>",
 "ssh_user": "ansible",
 "ssh_key": "~/.ssh/id_rsa"
}
```

`awx_container` and `jewel_container` default to `automation-controller-web` and
`automation-gateway`.

## Running

```
uv run --no-project --with requests python scenarios.py [scenario ...]
uv run --no-project --with requests python fault_scenarios.py [scenario ...]
python checks.py
```

With no names, `fault_scenarios.py` runs every fault scenario except
`f_p6_empty_team_manifest`. That one makes AWX delete every team it has from Jewel, and a
normal pull then restores them. Name it to run it.

Scenarios create objects and never clean them up, so use an install you can throw away. AWX's
own pull runs every 15 minutes and can interleave with a scenario.
