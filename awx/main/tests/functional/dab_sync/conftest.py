from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from django.apps import apps
from django.core.cache import cache
from django.db import connection
from django.test import Client
from rest_framework.views import APIView

from ansible_base.jwt_consumer.awx.auth import AwxJWTAuthentication
from ansible_base.rbac.managed import get_managed_role_constructors
from ansible_base.rbac.models import RoleDefinition
from ansible_base.resource_registry import apps as resource_registry_apps
from ansible_base.resource_registry import service_client

from awx.main import middleware
from awx.main.middleware import SettingsCacheMiddleware
from awx.main.models import Credential, Inventory, JobTemplate, Organization, User
from awx.main.tasks.system import periodic_resource_sync

from .fake_jewel import JEWEL_URL, SERVICE_INDEX_PATH, FakeJewel

# The managed role registry the AAP installer gives AWX
# (aap-containerized-installer roles/automationcontroller/templates/settings.py.j2)
PLATFORM_AUDITOR = {"platform_auditor": {"name": "Platform Auditor", "shortname": "sys_auditor"}}


@pytest.fixture
def jewel(settings, monkeypatch):
    """AWX deployed under a fake Jewel, configured the way the AAP installer configures AWX

    - RESOURCE_SERVER points at Jewel, so AWX accepts only Jewel's JWT (awx/settings/__init__.py)
    - the API is served under /api/controller/
    - Platform Auditor is a managed role definition
    - reverse sync is on. DAB connects its signals at startup only if a resource server is
      configured, which AWX's test settings do not do, so they are connected here and put back after.
    - requests are atomic, which the test settings' DATABASES drops

    Two middlewares are told about the test environment: SettingsCacheMiddleware clears AWX's
    settings wrapper cache, which is not reachable while pytest-django's settings fixture has the
    settings overridden, and MigrationRanCheckMiddleware looks for a migrations table, which
    --nomigrations does not create.
    """
    jewel = FakeJewel()
    settings.RESOURCE_SERVER = {"URL": JEWEL_URL, "SECRET_KEY": "fake-jewel-secret", "VALIDATE_HTTPS": False}
    settings.RESOURCE_SERVICE_PATH = SERVICE_INDEX_PATH
    settings.RESOURCE_SERVER_SYNC_ENABLED = True
    settings.ANSIBLE_BASE_JWT_KEY = jewel.public_key
    settings.ANSIBLE_BASE_MANAGED_ROLE_REGISTRY = PLATFORM_AUDITOR
    settings.OPTIONAL_API_URLPATTERN_PREFIX = "controller"
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_AUTHENTICATION_CLASSES": ["ansible_base.jwt_consumer.awx.auth.AwxJWTAuthentication"]}
    monkeypatch.setattr(APIView, "authentication_classes", [AwxJWTAuthentication])  # views read the setting when their class is made
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    cache.clear()  # cached JWT key, user and claims hashes
    jewel.awx = Client(raise_request_exception=False)
    resource_registry_apps.disconnect_resource_signals(sender=None)
    resource_registry_apps.connect_resource_signals(sender=None)
    try:
        with (
            mock.patch.object(service_client, "requests", SimpleNamespace(request=jewel.request, exceptions=requests.exceptions)),
            mock.patch.object(SettingsCacheMiddleware, "process_request", return_value=None),
            mock.patch.object(middleware, "is_migrating", return_value=False),
        ):
            yield jewel
    finally:
        resource_registry_apps.disconnect_resource_signals(sender=None)
        settings.RESOURCE_SERVER_SYNC_ENABLED = False
        resource_registry_apps.connect_resource_signals(sender=None)
    assert not jewel.unhandled, f"AWX made requests the fake Jewel has no route for: {jewel.unhandled}"


@pytest.fixture
def world(jewel, setup_managed_roles, credentialtype_ssh):
    """A small AAP install in sync: org o1 with team t1 (alice is a member), bob, a superuser, and AWX-only objects in o1

    t1 can execute job template jt1, bob can use inventory inv1, cred1 belongs to o1.
    """
    for constructor in get_managed_role_constructors(apps, PLATFORM_AUDITOR).values():
        constructor.get_or_create(apps)
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
    jewel.pushes.clear()
    return SimpleNamespace(admin=admin, alice=alice, bob=bob, org=org, team=team, inventory=inventory, jt=jt, credential=credential)


@pytest.fixture
def api(jewel):
    """AWX's API through its real request stack (middleware, authentication), returning 500s instead of raising"""
    return jewel.awx


@pytest.fixture
def pull():
    """Run AWX's periodic resource sync, the way the dispatcher does"""
    return periodic_resource_sync
