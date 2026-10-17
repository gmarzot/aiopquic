#!/bin/bash
set -e

# Routes through the network simulator.
/setup.sh

if [ "$ROLE" == "client" ]; then
    /wait-for-it.sh sim:57832 -s -t 30
fi

exec /venv/bin/python -u /qns/endpoint.py
