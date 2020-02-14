# Copyright (c) 2018 Red Hat, Inc.
# All Rights Reserved.

# Python
import logging

# Django
from django.utils.translation import ugettext_lazy as _

# Django REST Framework
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied


# AWX
# from awx.main.analytics import collectors
from awx.main.health.websockets import health
from awx.api import renderers

from awx.api.generics import (
    APIView,
)


logger = logging.getLogger('awx.main.analytics')


class HealthView(APIView):

    name = _('Health')
    swagger_topic = 'Health'

    renderer_classes = [renderers.PlainTextRenderer,
                        renderers.PrometheusJSONRenderer,
                        renderers.BrowsableAPIRenderer,]

    def get(self, request):
        ''' Show Health Details '''
        if (request.user.is_superuser or request.user.is_system_auditor):
            return Response(health().decode('UTF-8'))
        raise PermissionDenied()
