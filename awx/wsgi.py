# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

import logging
from awx import __version__ as tower_version

# Prepare the AWX environment.
from awx import prepare_env, MODE
from awx.main.tracing import init_tracing
from awx.settings.defaults import AWX_TRACER_API

prepare_env()

import django  # NOQA
from django.conf import settings  # NOQA
from django.urls import resolve  # NOQA
from django.core.wsgi import get_wsgi_application  # NOQA
import social_django  # NOQA

from opentelemetry.instrumentation.django import DjangoInstrumentor

from uwsgidecorators import postfork
from awx.settings.defaults import AWX_TRACER_API


"""
WSGI config for AWX project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/wsgi/
"""

if MODE == 'production':
    logger = logging.getLogger('awx.main.models.jobs')
    try:
        fd = open("/var/lib/awx/.tower_version", "r")
        if fd.read().strip() != tower_version:
            raise ValueError()
    except FileNotFoundError:
        pass
    except ValueError as e:
        logger.error("Missing or incorrect metadata for controller version.  Ensure controller was installed using the setup playbook.")
        raise Exception("Missing or incorrect metadata for controller version.  Ensure controller was installed using the setup playbook.") from e

DjangoInstrumentor().instrument()


@postfork
def init_tracing_api():
    """
    More detail on the need for the postfork decorator
    https://opentelemetry-python.readthedocs.io/en/latest/examples/fork-process-model/README.html

    This code doesn't actually have to live here. It can live anywhere that
    we know will be imported by uwsgi on init.
    """
    init_tracing(AWX_TRACER_API)


init_tracing(AWX_TRACER_API)

# Return the default Django WSGI application.
application = get_wsgi_application()
