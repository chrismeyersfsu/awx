"""
Ways AWX's copy of shared state goes wrong, applied straight to AWX's database so no signal or push runs.
"""

import uuid

from django.contrib.contenttypes.models import ContentType

from ansible_base.resource_registry.models import Resource, service_id


def _resource_qs(obj):
    return Resource.objects.filter(content_type=ContentType.objects.get_for_model(obj), object_id=str(obj.pk))


def corrupt_ansible_id(obj):
    """Give obj's Resource an ansible_id Jewel has never seen, returning it"""
    new_id = str(uuid.uuid4())
    assert _resource_qs(obj).update(ansible_id=new_id) == 1
    return new_id


def make_local(obj):
    """Mark obj's Resource as owned by AWX itself, as if it was never adopted by Jewel"""
    assert _resource_qs(obj).update(service_id=service_id()) == 1
