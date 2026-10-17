#!/bin/bash
set -e

# Routes through the network simulator.
/setup.sh

if [ "$ROLE" == "client" ]; then
    /wait-for-it.sh sim:57832 -s -t 30
fi

if [ "$QNS_PROTOCOL" == "webtransport" ]; then
    exec /venv/bin/python -u /qns/wt_endpoint.py
fi
exec /venv/bin/python -u /qns/endpoint.py
