from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from django.core.cache import cache
from django.db import connection
from django.test import Client

from ansible_base.rbac.models import RoleDefinition
from ansible_base.resource_registry import service_client

from awx.main import middleware
from awx.main.middleware import SettingsCacheMiddleware
from awx.main.models import Credential, Inventory, JobTemplate, Organization, User
from awx.main.tasks.system import periodic_resource_sync

from .fake_jewel import JEWEL_URL, SERVICE_INDEX_PATH, FakeJewel


@pytest.fixture
def jewel(settings):
    """AWX deployed under a fake Jewel: reverse sync on, JWT keys from the fake, DAB's client talking to it"""
    jewel = FakeJewel()
    settings.RESOURCE_SERVER = {"URL": JEWEL_URL, "SECRET_KEY": "fake-jewel-secret", "VALIDATE_HTTPS": False}
    settings.RESOURCE_SERVICE_PATH = SERVICE_INDEX_PATH
    settings.RESOURCE_SERVER_SYNC_ENABLED = True
    settings.ANSIBLE_BASE_JWT_KEY = jewel.public_key
    cache.clear()  # cached JWT key, user and claims hashes
    with mock.patch.object(service_client, "requests", SimpleNamespace(request=jewel.request, exceptions=requests.exceptions)):
        yield jewel
    assert not jewel.unhandled, f"AWX made requests the fake Jewel has no route for: {jewel.unhandled}"


@pytest.fixture
def world(jewel, setup_managed_roles, credentialtype_ssh):
    """A small AAP install in sync: org o1 with team t1 (alice is a member), bob, a superuser, and AWX-only objects in o1

    t1 can execute job template jt1, bob can use inventory inv1, cred1 belongs to o1.
    """
    admin = User.objects.create(username="admin", is_superuser=True)
    alice = User.objects.create(username="alice")
    bob = User.objects.create(username="bob")
    org = Organization.objects.create(name="o1")
    team = org.teams.create(name="t1")
    inventory = Inventory.objects.create(name="inv1", organization=org)
    jt = JobTemplate.objects.create(name="jt1", inventory=inventory)
    credential = Credential.objects.create(name="cred1", organization=org, credential_type=credentialtype_ssh)

    RoleDefinition.objects.get(name="Team Member").give_permission(alice, team)
    RoleDefinition.objects.get(name="JobTemplate Execute").give_permission(team, jt)
    RoleDefinition.objects.get(name="Inventory Use").give_permission(bob, inventory)

    jewel.adopt()
    jewel.calls.clear()
    return SimpleNamespace(admin=admin, alice=alice, bob=bob, org=org, team=team, inventory=inventory, jt=jt, credential=credential)


@pytest.fixture
def api(jewel, monkeypatch):
    """A real request stack (middleware, authentication classes), returning 500s instead of raising

    Requests are atomic like in production (awx/settings/defaults.py), which the test settings'
    DATABASES drops, so a failed request leaves nothing behind.

    Two middlewares are told about the test environment: SettingsCacheMiddleware clears AWX's
    settings wrapper cache, which is not reachable while pytest-django's settings fixture has the
    settings overridden, and MigrationRanCheckMiddleware looks for a migrations table, which
    --nomigrations does not create.
    """
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    with mock.patch.object(SettingsCacheMiddleware, "process_request", return_value=None), mock.patch.object(middleware, "is_migrating", return_value=False):
        yield Client(raise_request_exception=False)


@pytest.fixture
def pull():
    """Run AWX's periodic resource sync, the way the dispatcher does"""
    return periodic_resource_sync
