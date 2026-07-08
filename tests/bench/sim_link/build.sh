#!/usr/bin/env bash
# Build sim_link_bench. Run after ./build.sh (or ./build.sh --native).
#
# Output: tests/bench/sim_link/sim_link_bench
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PQ="${ROOT}/third_party/picoquic"
PT="${ROOT}/third_party/picotls"

if [ ! -f "${PQ}/build/libpicoquic-test.a" ]; then
    echo "error: picoquic libs not built. Run ./build.sh first." >&2
    exit 1
fi

# macOS Homebrew OpenSSL paths — Apple deprecated the system openssl,
# so libssl/libcrypto live under brew. build.sh assumes the
# same; we mirror that here.
EXTRA_CFLAGS=""
EXTRA_LDFLAGS=""
if [ "$(uname -s)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        for f in openssl@3 openssl@1.1; do
            P="$(brew --prefix "$f" 2>/dev/null || true)"
            if [ -n "$P" ] && [ -d "$P/lib" ]; then
                EXTRA_CFLAGS="-I${P}/include"
                EXTRA_LDFLAGS="-L${P}/lib"
                break
            fi
        done
    fi
    if [ -z "${EXTRA_LDFLAGS}" ]; then
        echo "error: could not locate Homebrew OpenSSL on macOS." \
             "brew install openssl@3 first." >&2
        exit 1
    fi
fi

CC=${CC:-cc}
${CC} -O3 -DNDEBUG ${EXTRA_CFLAGS} \
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
    ${EXTRA_LDFLAGS} \
    -lssl -lcrypto -lpthread -lm \
    -o "${SCRIPT_DIR}/sim_link_bench"

echo "built: ${SCRIPT_DIR}/sim_link_bench"
