#!/usr/bin/env bash
# Build picotls and picoquic (incl. native test drivers picoquic_ct /
# picohttp_ct) from vendored submodules. Run before
# `pip install -e .` or `python -m build`.
#
# Sources:
#   third_party/picotls   (h2o/picotls submodule)
#   third_party/picoquic  (private-octopus/picoquic submodule)
#
# Outputs (under third_party/picoquic/build/):
#   picotls-build/libpicotls-*.a    picotls static libs
#   libpicoquic-core.a etc.         picoquic static libs
#   picoquic_ct, picohttp_ct        native test drivers (used by
#                                   tests/test_native_picoquic.py)

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

# --- Apply local picoquic patches (PRs not merged upstream yet) ---
# Patches live in patches/picoquic-*.patch and are applied to the
# vendored submodule before cmake. Skipped when AIOPQUIC_SKIP_PATCHES=1.
# Idempotent: `git apply --check` is used to detect already-applied
# patches and skip them, so re-running this script is safe.
PATCH_DIR="${SCRIPT_DIR}/patches"
if [ "${AIOPQUIC_SKIP_PATCHES:-0}" = "1" ]; then
    echo -e "${COLOR_GREEN}AIOPQUIC_SKIP_PATCHES=1: skipping patch application${COLOR_OFF}"
elif [ -d "${PATCH_DIR}" ]; then
    shopt -s nullglob
    for patch in "${PATCH_DIR}"/*.patch; do
        name="$(basename "${patch}")"
        if git -C "${PICOQUIC_DIR}" apply --check "${patch}" >/dev/null 2>&1; then
            echo -e "${COLOR_GREEN}applying ${name}${COLOR_OFF}"
            git -C "${PICOQUIC_DIR}" apply "${patch}"
        elif git -C "${PICOQUIC_DIR}" apply --check --reverse "${patch}" >/dev/null 2>&1; then
            echo -e "${COLOR_GREEN}${name}: already applied (skipping)${COLOR_OFF}"
        else
            echo -e "${COLOR_RED}${name}: cannot apply cleanly AND not already applied — likely upstream-merged or conflicts; remove the patch if so${COLOR_OFF}" >&2
            exit 1
        fi
    done
    shopt -u nullglob
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

# Allow callers to override the picoquic+picotls compile flags. CI
# wheel builds set this via the cibuildwheel environment block to
# pin perf-relevant flags (frame pointer, stack protector, unwind
# tables) so wheels match what local source builds achieve. Empty
# default keeps cmake's own Release defaults (-O3 -DNDEBUG).
PICOQUIC_C_FLAGS="${PICOQUIC_C_FLAGS:-}"
CMAKE_FLAG_ARGS=()
if [ -n "${PICOQUIC_C_FLAGS}" ]; then
    CMAKE_FLAG_ARGS+=("-DCMAKE_C_FLAGS_RELEASE=${PICOQUIC_C_FLAGS}")
fi

# Optional: enable io_uring submission for high-pps Linux workloads.
# EXPERIMENTAL / DORMANT in aiopquic — picoquic supports io_uring
# via picoquic_packet_loop_uring, but aiopquic's worker thread does
# not call that path today, so enabling this currently has no
# runtime effect. Scaffolding lives here for when the worker is
# migrated. Default OFF; Linux-only.
#
# When AIOPQUIC_IO_URING=1:
#   - vendored third_party/liburing submodule is auto-init'd
#   - liburing is built static-only into third_party/liburing/install
#   - cmake gets -DWITH_IO_URING=ON
#   - setup.py mirrors -DPICOQUIC_WITH_IO_URING into the Cython
#     compile (REQUIRED — struct layouts in picoquic_packet_loop.h
#     are conditional on this define; mismatch corrupts thread_ready
#     and the network thread silently never starts).
PICOQUIC_IO_URING_ARGS=()
LIBURING_DIR="${SCRIPT_DIR}/third_party/liburing"
LIBURING_INSTALL="${LIBURING_DIR}/install"
LIBURING_STATIC="${LIBURING_INSTALL}/lib/liburing.a"
if [ "${AIOPQUIC_IO_URING:-0}" = "1" ]; then
    if [ "$(uname -s)" != "Linux" ]; then
        echo -e "${COLOR_RED}ERROR: AIOPQUIC_IO_URING=1 set but io_uring is Linux-only.${COLOR_OFF}" >&2
        echo -e "${COLOR_RED}Unset AIOPQUIC_IO_URING and re-run on this platform ($(uname -s)).${COLOR_OFF}" >&2
        exit 1
    fi
    echo -e "${COLOR_GREEN}AIOPQUIC_IO_URING=1: enabling -DWITH_IO_URING=ON (EXPERIMENTAL — dormant in aiopquic worker today)${COLOR_OFF}"
    # Auto-init the liburing submodule if not yet checked out.
    if [ ! -f "${LIBURING_DIR}/configure" ]; then
        echo -e "${COLOR_GREEN}Initializing third_party/liburing submodule...${COLOR_OFF}"
        git -C "${SCRIPT_DIR}" submodule update --init third_party/liburing
    fi
    # Build liburing (static only) into install/ if not already built.
    if [ ! -f "${LIBURING_STATIC}" ]; then
        echo -e "${COLOR_GREEN}Building liburing static lib into ${LIBURING_INSTALL}...${COLOR_OFF}"
        (
            cd "${LIBURING_DIR}"
            ./configure \
                --prefix="${LIBURING_INSTALL}" \
                --includedir="${LIBURING_INSTALL}/include" \
                --libdir="${LIBURING_INSTALL}/lib" \
                --libdevdir="${LIBURING_INSTALL}/lib" \
                --mandir="${LIBURING_INSTALL}/man" \
                --datadir="${LIBURING_INSTALL}/share"
            make -j "${NPROC}"
            make install
            # Force static link in picoquic + Cython by removing the
            # shared library variants from the install prefix; cmake's
            # find_library will then resolve to liburing.a.
            rm -f "${LIBURING_INSTALL}/lib"/liburing.so*
        )
    fi
    PICOQUIC_IO_URING_ARGS+=("-DWITH_IO_URING=ON")
    # Inject our install paths so picoquic's cmake (and the Cython
    # build via setup.py inheriting the env) finds our liburing first
    # rather than any system liburing-dev that may be installed.
    export CMAKE_PREFIX_PATH="${LIBURING_INSTALL}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
    export CFLAGS="-I${LIBURING_INSTALL}/include${CFLAGS:+ ${CFLAGS}}"
fi

# Optional: host-tuned performance flags. AIOPQUIC_PERF=1 enables
# every portable optimization safe for LOCAL builds:
#   - DISABLE_DEBUG_PRINTF (every platform; strips dbg branches)
#   - PTLS Fusion AES-GCM (x86_64 only; picotls runtime-CPUIDs
#     AES-NI + PCLMULQDQ so the lib is safe to bake in even on
#     hosts that lack them — runtime falls back to picotls openssl)
#   - -O3 -march=native -flto (every platform; host-tuned ISA)
#
# Default OFF: -march=native produces machine-specific binaries
# unsuitable for distributable wheels. Wheel builds get a separate,
# portable baseline (e.g. -march=x86-64-v3) via cibuildwheel env.
PICOQUIC_PERF_ARGS=()
PICOTLS_PERF_ARGS=()
if [ "${AIOPQUIC_PERF:-0}" = "1" ]; then
    echo -e "${COLOR_GREEN}AIOPQUIC_PERF=1: host-tuned perf flags enabled (Fusion if x86_64, DISABLE_DEBUG_PRINTF, -O3 -march=native -flto)${COLOR_OFF}"
    PICOQUIC_PERF_ARGS+=("-DDISABLE_DEBUG_PRINTF=ON")
    PERF_ARCH="$(uname -m)"
    if [ "${PERF_ARCH}" = "x86_64" ] || [ "${PERF_ARCH}" = "amd64" ]; then
        PICOQUIC_PERF_ARGS+=("-DPTLS_WITH_FUSION=ON")
        PICOTLS_PERF_ARGS+=("-DWITH_FUSION=ON")
    fi
    if [ "$(uname -s)" = "Darwin" ] && [ "${PERF_ARCH}" = "arm64" ]; then
        _PERF_CC_FLAGS="-O3 -DNDEBUG -mcpu=native -flto"
    else
        _PERF_CC_FLAGS="-O3 -DNDEBUG -march=native -flto -fno-plt"
    fi
    PICOQUIC_C_FLAGS="${_PERF_CC_FLAGS}${PICOQUIC_C_FLAGS:+ ${PICOQUIC_C_FLAGS}}"
    # PICOQUIC_C_FLAGS was already consumed above into CMAKE_FLAG_ARGS,
    # so re-derive it here for both picotls + picoquic to see the
    # AIOPQUIC_PERF values.
    CMAKE_FLAG_ARGS=("-DCMAKE_C_FLAGS_RELEASE=${PICOQUIC_C_FLAGS}")
fi

echo -e "${COLOR_GREEN}Building picotls from ${PICOTLS_DIR}...${COLOR_OFF}"
mkdir -p "${PTLS_BUILD_DIR}"
cmake -S "${PICOTLS_DIR}" -B "${PTLS_BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DWITH_FUSION=OFF \
    ${CMAKE_FLAG_ARGS[@]+"${CMAKE_FLAG_ARGS[@]}"} \
    ${CMAKE_OPENSSL_ARGS[@]+"${CMAKE_OPENSSL_ARGS[@]}"} \
    ${PICOTLS_PERF_ARGS[@]+"${PICOTLS_PERF_ARGS[@]}"}
cmake --build "${PTLS_BUILD_DIR}" -j "${NPROC}"
echo -e "${COLOR_GREEN}picotls build complete.${COLOR_OFF}"

# Picotls upstream has no install rules for its static libs, so feed
# their absolute paths to picoquic's FindPTLS.cmake as cache vars
# (also sidesteps a quirk in FindPTLS where PTLS_PREFIX/include is
# mis-globbed).
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

# Native test drivers (picoquic_ct, picohttp_ct) are built alongside
# the libraries so that `pytest -m native` validates picoquic itself
# on every submodule bump. Adds ~25s to build time; negligible vs the
# value of catching upstream regressions early.
cmake -S "${PICOQUIC_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -Dpicoquic_BUILD_TESTS=ON \
    -DBUILD_DEMO=OFF \
    -DBUILD_LOGREADER=OFF \
    -DBUILD_HTTP=ON \
    -DBUILD_LOGLIB=ON \
    -DPTLS_WITH_FUSION=OFF \
    -DPTLS_INCLUDE_DIR="${PICOTLS_DIR}/include" \
    -DPTLS_CORE_LIBRARY="${PTLS_CORE_LIB}" \
    -DPTLS_OPENSSL_LIBRARY="${PTLS_OPENSSL_LIB}" \
    -DPTLS_MINICRYPTO_LIBRARY="${PTLS_MINICRYPTO_LIB}" \
    ${CMAKE_FLAG_ARGS[@]+"${CMAKE_FLAG_ARGS[@]}"} \
    ${CMAKE_OPENSSL_ARGS[@]+"${CMAKE_OPENSSL_ARGS[@]}"} \
    ${PICOQUIC_IO_URING_ARGS[@]+"${PICOQUIC_IO_URING_ARGS[@]}"} \
    ${PICOQUIC_PERF_ARGS[@]+"${PICOQUIC_PERF_ARGS[@]}"}

cmake --build "${BUILD_DIR}" -j "${NPROC}" --target picoquic-core picohttp-core picoquic-log
cmake --build "${BUILD_DIR}" -j "${NPROC}" --target picoquic_ct picohttp_ct

PICOQUIC_LIB=$(find "${BUILD_DIR}" -name "libpicoquic-core.a" -print -quit 2>/dev/null || true)
if [ -z "${PICOQUIC_LIB}" ]; then
    echo -e "${COLOR_RED}ERROR: libpicoquic-core.a not found${COLOR_OFF}" >&2
    exit 1
fi

echo -e "${COLOR_GREEN}picoquic build complete.${COLOR_OFF}"
echo "Static libraries:"
find "${BUILD_DIR}" -name "lib*.a" -exec ls -la {} \; 2>/dev/null || true
echo "Native test drivers:"
ls -la "${BUILD_DIR}/picoquic_ct" "${BUILD_DIR}/picohttp_ct" 2>/dev/null || true
