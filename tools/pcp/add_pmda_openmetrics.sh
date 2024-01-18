#!/bin/bash
/usr/libexec/pcp/bin/pmcd -f &
export PMCD_PID=$!
sleep 5
cd /var/lib/pcp/pmdas/openmetrics
./Install
sleep 2
kill ${PMCD_PID}
sleep 4
