#!/usr/bin/env bash
# Build picotls and picoquic as static libraries from vendored submodules.
# Run before `pip install -e .` or `python -m build`.
#
# Sources:
#   third_party/picotls   (h2o/picotls submodule)
#   third_party/picoquic  (private-octopus/picoquic submodule)
#
# Outputs (under third_party/picoquic/build/):
#   picotls-build/libpicotls-*.a   picotls static libs
#   libpicoquic-core.a etc.        picoquic static libs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PICOQUIC_DIR="${SCRIPT_DIR}/third_party/picoquic"
PICOTLS_DIR="${SCRIPT_DIR}/third_party/picotls"
BUILD_DIR="${PICOQUIC_DIR}/build"

NPROC=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

COLOR_GREEN="\033[0;32m"
COLOR_RED="\033[0;31m"
COLOR_OFF="\033[0m"

# --- Sanity: submodules present ---
if [ ! -f "${PICOQUIC_DIR}/CMakeLists.txt" ]; then
    echo -e "${COLOR_RED}ERROR: picoquic submodule missing at ${PICOQUIC_DIR}${COLOR_OFF}" >&2
    echo "  Run: git submodule update --init --recursive" >&2
    exit 1
fi
if [ ! -f "${PICOTLS_DIR}/CMakeLists.txt" ]; then
    echo -e "${COLOR_RED}ERROR: picotls submodule missing at ${PICOTLS_DIR}${COLOR_OFF}" >&2
    echo "  Run: git submodule update --init --recursive" >&2
    exit 1
fi

# --- Locate OpenSSL (Homebrew on macOS, system on Linux) ---
CMAKE_OPENSSL_ARGS=()
if [ -z "${OPENSSL_ROOT_DIR:-}" ] && [ "$(uname -s)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        for pkg in openssl@3 openssl@1.1; do
            prefix="$(brew --prefix "${pkg}" 2>/dev/null || true)"
            if [ -n "${prefix}" ] && [ -d "${prefix}" ]; then
                export OPENSSL_ROOT_DIR="${prefix}"
                break
            fi
        done
    fi
fi
if [ -n "${OPENSSL_ROOT_DIR:-}" ]; then
    echo -e "${COLOR_GREEN}Using OPENSSL_ROOT_DIR=${OPENSSL_ROOT_DIR}${COLOR_OFF}"
    CMAKE_OPENSSL_ARGS+=("-DOPENSSL_ROOT_DIR=${OPENSSL_ROOT_DIR}")
fi

# --- Step 1: Build picotls from submodule ---
PTLS_BUILD_DIR="${BUILD_DIR}/picotls-build"

echo -e "${COLOR_GREEN}Building picotls from ${PICOTLS_DIR}...${COLOR_OFF}"
mkdir -p "${PTLS_BUILD_DIR}"
cmake -S "${PICOTLS_DIR}" -B "${PTLS_BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DWITH_FUSION=OFF \
    "${CMAKE_OPENSSL_ARGS[@]}"
cmake --build "${PTLS_BUILD_DIR}" -j "${NPROC}"
echo -e "${COLOR_GREEN}picotls build complete.${COLOR_OFF}"

# Picotls upstream has no install rules for its static libs, so feed
# their absolute paths to picoquic's FindPTLS.cmake as cache vars.
PTLS_CORE_LIB="${PTLS_BUILD_DIR}/libpicotls-core.a"
PTLS_OPENSSL_LIB="${PTLS_BUILD_DIR}/libpicotls-openssl.a"
PTLS_MINICRYPTO_LIB="${PTLS_BUILD_DIR}/libpicotls-minicrypto.a"
for lib in "${PTLS_CORE_LIB}" "${PTLS_OPENSSL_LIB}" "${PTLS_MINICRYPTO_LIB}"; do
    if [ ! -f "${lib}" ]; then
        echo -e "${COLOR_RED}ERROR: expected ${lib} after picotls build${COLOR_OFF}" >&2
        exit 1
    fi
done

# --- Step 2: Build picoquic against our picotls ---
echo -e "${COLOR_GREEN}Building picoquic in ${BUILD_DIR}...${COLOR_OFF}"
mkdir -p "${BUILD_DIR}"
cmake -S "${PICOQUIC_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -Dpicoquic_BUILD_TESTS=OFF \
    -DBUILD_DEMO=OFF \
    -DBUILD_LOGREADER=OFF \
    -DBUILD_HTTP=ON \
    -DBUILD_LOGLIB=ON \
    -DPTLS_INCLUDE_DIR="${PICOTLS_DIR}/include" \
    -DPTLS_CORE_LIBRARY="${PTLS_CORE_LIB}" \
    -DPTLS_OPENSSL_LIBRARY="${PTLS_OPENSSL_LIB}" \
    -DPTLS_MINICRYPTO_LIBRARY="${PTLS_MINICRYPTO_LIB}" \
    "${CMAKE_OPENSSL_ARGS[@]}"
cmake --build "${BUILD_DIR}" -j "${NPROC}"

PICOQUIC_LIB=$(find "${BUILD_DIR}" -name "libpicoquic-core.a" -print -quit 2>/dev/null || true)
if [ -z "${PICOQUIC_LIB}" ]; then
    echo -e "${COLOR_RED}ERROR: libpicoquic-core.a not found${COLOR_OFF}" >&2
    exit 1
fi

echo -e "${COLOR_GREEN}picoquic build complete.${COLOR_OFF}"
echo "Static libraries:"
find "${BUILD_DIR}" -name "lib*.a" -exec ls -la {} \; 2>/dev/null || true
