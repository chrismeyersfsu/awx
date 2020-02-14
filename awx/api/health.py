# Copyright (c) 2017 Ansible, Inc.
# All Rights Reserved.

from django.conf.urls import url

from awx.api.views import (
    HealthView
)


urls = [
    url(r'^$', HealthView.as_view(), name='health_view'),
]

__all__ = ['urls']
