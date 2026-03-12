#!/bin/bash
# Build picoquic and its dependencies (picotls) as static libraries.
# This must be run before `pip install -e .` or `python setup.py build_ext`.
#
# Uses picotls source from the original local picoquic build (_deps/picotls-src)
# if available, otherwise fetches it via cmake.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PICOQUIC_DIR="${SCRIPT_DIR}/third_party/picoquic"
BUILD_DIR="${PICOQUIC_DIR}/build"
# Use picotls source from the original local repo if available
LOCAL_PICOQUIC="/home/gmarzot/Projects/moq/picoquic"
PTLS_SRC="${LOCAL_PICOQUIC}/_deps/picotls-src"
NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

COLOR_GREEN="\033[0;32m"
COLOR_RED="\033[0;31m"
COLOR_OFF="\033[0m"

if [ ! -d "${PICOQUIC_DIR}/picoquic" ]; then
    echo -e "${COLOR_RED}ERROR: picoquic source not found at ${PICOQUIC_DIR}${COLOR_OFF}"
    echo "  Run: git submodule update --init --recursive"
    exit 1
fi

# --- Step 1: Build picotls ---
PTLS_BUILD_DIR="${BUILD_DIR}/picotls-build"
PTLS_INSTALL="${BUILD_DIR}/picotls-install"

if [ -d "${PTLS_SRC}" ]; then
    echo -e "${COLOR_GREEN}Building picotls from ${PTLS_SRC}...${COLOR_OFF}"
    mkdir -p "${PTLS_BUILD_DIR}"
    cd "${PTLS_BUILD_DIR}"
    cmake "${PTLS_SRC}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DCMAKE_INSTALL_PREFIX="${PTLS_INSTALL}"
    cmake --build . -j "${NPROC}"
    cmake --install . 2>/dev/null || true
    echo -e "${COLOR_GREEN}picotls build complete.${COLOR_OFF}"
else
    echo -e "${COLOR_RED}WARNING: picotls source not found at ${PTLS_SRC}${COLOR_OFF}"
    echo "Will try PICOQUIC_FETCH_PTLS=ON (requires network)."
fi

# --- Step 2: Build picoquic ---
echo -e "${COLOR_GREEN}Building picoquic in ${BUILD_DIR}...${COLOR_OFF}"

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

CMAKE_EXTRA=""
if [ -d "${PTLS_INSTALL}" ]; then
    # Point FindPTLS.cmake at our built picotls
    CMAKE_EXTRA="-DPTLS_PREFIX=${PTLS_INSTALL} -DCMAKE_PREFIX_PATH=${PTLS_INSTALL}"
elif [ -d "${PTLS_SRC}" ]; then
    # Fallback: point at the build dir directly
    CMAKE_EXTRA="-DPTLS_PREFIX=${PTLS_BUILD_DIR} -DPTLS_INCLUDE_DIR=${PTLS_SRC}/include"
else
    CMAKE_EXTRA="-DPICOQUIC_FETCH_PTLS=ON"
fi

cmake "${PICOQUIC_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -Dpicoquic_BUILD_TESTS=OFF \
    -DBUILD_DEMO=OFF \
    -DBUILD_LOGREADER=OFF \
    -DBUILD_HTTP=ON \
    -DBUILD_LOGLIB=ON \
    ${CMAKE_EXTRA}

cmake --build . -j "${NPROC}"

# Verify key outputs exist
if [ ! -f "${BUILD_DIR}/picoquic/libpicoquic-core.a" ]; then
    echo -e "${COLOR_RED}ERROR: libpicoquic-core.a not found${COLOR_OFF}"
    exit 1
fi

echo -e "${COLOR_GREEN}picoquic build complete.${COLOR_OFF}"
echo "Libraries:"
ls -la "${BUILD_DIR}"/picoquic/lib*.a 2>/dev/null || true
ls -la "${PTLS_BUILD_DIR}"/lib*.a 2>/dev/null || true
ls -la "${PTLS_INSTALL}"/lib/lib*.a 2>/dev/null || true
