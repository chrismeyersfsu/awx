FROM quay.io/centos/centos:stream9

ENV LANGUAGE en_US:en
ENV AWX_USER ''
ENV AWX_PASS ''
env AWX_HOST ''
env AWX_PROTO 'http'
env AWX_PORT '8013'

USER root

RUN dnf -y update && dnf install -y \
    bind-utils \
    pcp-zeroconf \
    pcp-system-tools \
    procps \
    pcp-pmda-openmetrics

# pcpd
RUN mkdir -p /run/pcp   # pcp will fail to start without this

RUN mkdir -p /tmp/pmcd  # starting pcp lays down log files in the dir you run it
WORKDIR /tmp/pmcd

# openmetrics, also boostraps pcp for the first time
RUN rm /var/lib/pcp/pmdas/openmetrics/config.d/grafana.url
COPY add_pmda_openmetrics.sh add_pmda_openmetrics.sh
RUN ./add_pmda_openmetrics.sh

WORKDIR /

COPY entrypoint.sh entrypoint.sh
RUN chmod +x entrypoint.sh

#ENTRYPOINT ["tail", "-f", "/dev/null"]
#ENTRYPOINT ["/usr/libexec/pcp/bin/pmcd", "-f"]
ENTRYPOINT ["/entrypoint.sh"]
