#!/bin/bash
# Build the native picoquic test drivers (picoquic_ct, picohttp_ct).
# Opt-in: not part of the standard build_picoquic.sh because the
# native test build is heavy and rarely needed for aiopquic dev.
#
# After this completes, `pytest -m native` will run a smoke list
# of native tests via tests/test_native_picoquic.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PICOQUIC_DIR="${SCRIPT_DIR}/third_party/picoquic"
BUILD_DIR="${PICOQUIC_DIR}/build"
NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

if [ ! -f "${BUILD_DIR}/CMakeCache.txt" ]; then
    echo "ERROR: run ./build_picoquic.sh first."
    exit 1
fi

cd "${BUILD_DIR}"

# Reconfigure with tests enabled, keeping picotls path from the
# original configure step (CMakeCache preserves -DPTLS_PREFIX).
cmake "${PICOQUIC_DIR}" -Dpicoquic_BUILD_TESTS=ON
cmake --build . -j "${NPROC}" --target picoquic_ct picohttp_ct

ls -la "${BUILD_DIR}/picoquic_ct" "${BUILD_DIR}/picohttp_ct"
echo "Native picoquic test drivers built."
