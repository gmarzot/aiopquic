# Interop Runner endpoints. Context: qns/, with the cp312 manylinux wheel
# under test in qns/wheelhouse/. Targets: quic (default), webtransport.
FROM martenseemann/quic-network-simulator-endpoint:latest AS base

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY wheelhouse/ /tmp/wheelhouse/
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir /tmp/wheelhouse/aiopquic-*-cp312-*.whl \
 && rm -rf /tmp/wheelhouse

COPY endpoint.py wt_endpoint.py /qns/
COPY run_endpoint.sh /run_endpoint.sh

FROM base AS webtransport
ENV QNS_PROTOCOL=webtransport

FROM base AS quic
ENV QNS_PROTOCOL=quic
