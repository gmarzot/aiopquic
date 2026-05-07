#!/usr/bin/env bash
# Build sim_link_bench. Run after build_picoquic.sh.
#
# Output: tests/bench/sim_link/sim_link_bench
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PQ="${ROOT}/third_party/picoquic"
PT="${ROOT}/third_party/picotls"

if [ ! -f "${PQ}/build/libpicoquic-test.a" ]; then
    echo "error: picoquic libs not built. Run ./build_picoquic.sh first." >&2
    exit 1
fi

CC=${CC:-gcc}
${CC} -O3 -DNDEBUG \
    -I"${PQ}/picoquic" -I"${PQ}/picoquictest" -I"${PQ}/picohttp" \
    -I"${PQ}/loglib" -I"${PT}/include" \
    "${SCRIPT_DIR}/sim_link_bench.c" \
    "${PQ}/build/libpicoquic-test.a" \
    "${PQ}/build/libpicohttp-core.a" \
    "${PQ}/build/libpicoquic-log.a" \
    "${PQ}/build/libpicoquic-core.a" \
    "${PQ}/build/picotls-build/libpicotls-openssl.a" \
    "${PQ}/build/picotls-build/libpicotls-minicrypto.a" \
    "${PQ}/build/picotls-build/libpicotls-core.a" \
    -lssl -lcrypto -lpthread -lm \
    -o "${SCRIPT_DIR}/sim_link_bench"

echo "built: ${SCRIPT_DIR}/sim_link_bench"
