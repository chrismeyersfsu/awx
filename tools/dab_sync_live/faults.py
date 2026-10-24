"""
Fault injection for the live harness.

Faults go in at the same seams the fake uses, inside one shell process on the instance, so the
rest of AAP runs as it is:

- AWX's requests to Jewel (the pull, push on save, the jwt_claims fetch at login) go through
  DAB's BaseServiceClient, which calls requests.request. awx_faulty() runs code in an
  awx-manage shell with requests.request wrapped: a matching request gets an injected answer,
  a changed copy of Jewel's real answer, or an exception, and every request is recorded.
- Jewel's pushes come from its API views through AllServicesClient._make_request.
  jewel_api() makes API calls through Jewel's real views in an aap-gateway-manage shell with
  that method replaced, so the pushes are held instead of sent. Dropping them is a lost push,
  and jewel_deliver() sends them to AWX later, in any order. Held pushes reach no service
  (EDA, Hub and the rest get them at their next pull too).
- AWX's API is called in-process with DRF's APIClient, as admin or with a JWT that Jewel
  signed (jewel_jwt()), so it sees the injected faults. The web container's API is untouched.
"""

import json

from live import AWX_CONTAINER, JEWEL_CONTAINER, _container_python

AWX_FAULT_PREAMBLE = """
import fnmatch
import io
from urllib.parse import urlsplit

import requests
from rest_framework.test import APIClient

from awx.main.models import User
from awx.main.tasks.system import periodic_resource_sync

CALLS = []
_real_request = requests.request


def _injected(fault):
    body = fault.get("body", "")
    content = (body if isinstance(body, str) else json.dumps(body)).encode()
    response = requests.Response()
    response.status_code = fault["status"]
    response._content = content
    response.raw = io.BytesIO(content)
    response.headers.update(fault.get("headers") or {"Content-Type": "application/json"})
    return response


def _faulty_request(method, url, **kwargs):
    path = urlsplit(url).path
    for fault in FAULTS:
        if fault.get("method", method).upper() != method.upper() or not fnmatch.fnmatch(path, fault["path"]):
            continue
        if fault.get("exc"):
            CALLS.append([method.upper(), path, fault["exc"]])
            raise getattr(requests, fault["exc"])("injected: Jewel cannot be reached")
        if "status" in fault:
            response = _injected(fault)
        else:
            response = _real_request(method, url, **kwargs)
            for header in fault.get("drop_headers", []):
                response.headers.pop(header, None)
            response.headers.update(fault.get("headers", {}))
        CALLS.append([method.upper(), path, response.status_code, "injected"])
        return response
    response = _real_request(method, url, **kwargs)
    CALLS.append([method.upper(), path, response.status_code])
    return response


requests.request = _faulty_request


def api_call(method, path, body=None, user=None, jwt=None):
    '''AWX's API in this process: as an AWX user, or with a JWT Jewel signed'''
    client = APIClient()
    headers = {}
    if user:
        client.force_authenticate(User.objects.get(username=user))
    if jwt:
        headers["HTTP_X_DAB_JW_TOKEN"] = jwt
    response = getattr(client, method.lower())(path, body, format="json", **headers)
    try:
        content = response.json() if response.content else None
    except (TypeError, ValueError):
        content = response.content.decode()[:500]
    return [response.status_code, content]


def pull():
    try:
        periodic_resource_sync()
        return {"raised": None}
    except Exception as e:
        return {"raised": f"{type(e).__name__}: {e}"[:300]}
"""


def awx_faulty(code, faults=()):
    """Run code in AWX with Jewel's answers faulted

    The code sets `result`. Each fault is a dict: `path` (an fnmatch glob on the URL path),
    optional `method`, and one of `status` (with `body`, `headers`) for an injected answer,
    `exc` (a requests exception name) for a failure to connect, or `headers`/`drop_headers`
    to change Jewel's real answer. Returns {"result": ..., "calls": [[method, path, status], ...]}.
    """
    script = f"FAULTS = json.loads({json.dumps(list(faults))!r})\n" + AWX_FAULT_PREAMBLE + code
    script += "\nprint('@@' + json.dumps({'result': result, 'calls': CALLS}, default=str))\n"
    return _container_python(AWX_CONTAINER, "awx-manage", script)


JEWEL_HOLD_PREAMBLE = """
from django.contrib.auth import get_user_model
from django.test.utils import override_settings
from rest_framework.test import APIClient

from aap_gateway_api.utils.resources_client import AllServicesClient

HELD = []


def _hold(self, method, path, data=None, params=None):
    HELD.append({"method": method, "path": path, "data": data, "params": params})
    return {}


AllServicesClient._make_request = _hold
"""


def jewel_api(*requests):
    """Make Jewel API calls through its views, holding the pushes they make

    Each request is (method, path under /api/gateway/v1/, body or None). Returns
    {"responses": [[status, body], ...], "held": [push, ...]}.
    """
    code = (
        JEWEL_HOLD_PREAMBLE
        + f"""
api = APIClient()
api.force_authenticate(get_user_model().objects.get(username="admin"))
responses = []
with override_settings(ALLOWED_HOSTS=["*"]):
    for method, path, body in {list(requests)!r}:
        r = getattr(api, method.lower())("/api/gateway/v1/" + path.lstrip("/"), body, format="json")
        try:
            content = r.json() if r.content else None
        except (TypeError, ValueError):
            content = r.content.decode()[:500]
        responses.append([r.status_code, content])
print("@@" + json.dumps({{"responses": responses, "held": HELD}}, default=str))
"""
    )
    return _container_python(JEWEL_CONTAINER, "aap-gateway-manage", code)


def jewel_deliver(pushes):
    """Send held pushes to AWX, in the order given, and return AWX's answers as [status, body]"""
    code = f"""
from aap_gateway_api.utils.resources_client import AllServicesClient

client = AllServicesClient(wait_for_response=True, service_filter={{"service_cluster__service_type__name": "controller"}})
answers = []
for push in json.loads({json.dumps(pushes, default=str)!r}):
    [response] = client._make_request(push["method"], push["path"], data=push["data"], params=push["params"]).values()
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    answers.append([push["method"], push["path"], response.status_code, body])
print("@@" + json.dumps(answers, default=str))
"""
    return _container_python(JEWEL_CONTAINER, "aap-gateway-manage", code)


def jewel_jwt(username):
    """The JWT Jewel's proxy would send AWX for this user's request"""
    code = f"""
from django.contrib.auth import get_user_model
from aap_gateway_api.utils.jwt_token import create_signed_jwt
print("@@" + json.dumps(create_signed_jwt(get_user_model().objects.get(username={username!r}))))
"""
    return _container_python(JEWEL_CONTAINER, "aap-gateway-manage", code)
