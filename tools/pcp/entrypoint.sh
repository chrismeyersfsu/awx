#!/bin/bash

# HACK: Django does not like the header Host: awx_1
# It will throw the below error
#
# django.core.exceptions.DisallowedHost: Invalid HTTP_HOST header: 'awx_1:8013'. The domain name provided is not valid according to RFC 1034/1035.
#
# A root cause fix would be to change the container names from awx_1 to awx-1
# but this might have a ripple effect.

AWX_IP=$(dig +short ${AWX_HOST})

echo "${AWX_PROTO}://${AWX_USER}:${AWX_PASS}@${AWX_IP}:${AWX_PORT}/api/v2/metrics" > /var/lib/pcp/pmdas/openmetrics/config.d/awx.url

/usr/libexec/pcp/bin/pmcd -f $@

# pminfo openmetrics
# Run the above to test that things are hooked up correctly.
