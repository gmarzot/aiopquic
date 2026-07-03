#!/usr/bin/env bash
# aiopquic build front door.
#
# Guarantee: after a default `./build.sh`, the aiopquic that is
# *installed and imported* reflects the *current branch/commit source*
# (submodules reconciled, native libs current, extension relinked),
# host-tuned — or the script fails loudly.
#
# Common use:
#   ./build.sh            reconcile submodules -> build native (if stale)
#                         -> relink extension -> verify. Idempotent: a
#                         matching fingerprint skips the native compile.
#   ./build.sh --check    doctor: report drift / staleness / wrong-flavor
#                         and exit nonzero, changing NOTHING. Use as a
#                         pre-benchmark / CI gate.
#   ./build.sh --force    rebuild native even if the fingerprint matches.
#   ./build.sh --native   only compile native libs (picotls/picoquic +
#                         test drivers); no reconcile, no relink. This is
#                         the phase build_picoquic.sh delegates to.
#   ./build.sh --install  only relink the extension + verify.
#   ./build.sh --verify   only check imported == this source tree + Fusion.
#
# Flavor is driven by the same env as before, passed through to --native:
#   AIOPQUIC_PERF=0         opt out of host-tuned flags (debug builds)
#   AIOPQUIC_WHEEL_BUILD=1  portable build (caller drives PICOQUIC_C_FLAGS)
#   AIOPQUIC_IO_URING=1     enable io_uring (Linux; EXPERIMENTAL)
#   AIOPQUIC_SKIP_PATCHES=1 skip patches/*.patch application
#
# Outputs (under third_party/picoquic/build/):
#   picotls-build/libpicotls-*.a    picotls static libs
#   libpicoquic-core.a etc.         picoquic static libs
#   picoquic_ct, picohttp_ct        native test drivers

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PICOQUIC_DIR="${SCRIPT_DIR}/third_party/picoquic"
PICOTLS_DIR="${SCRIPT_DIR}/third_party/picotls"
LIBURING_DIR="${SCRIPT_DIR}/third_party/liburing"
BUILD_DIR="${PICOQUIC_DIR}/build"
PATCH_DIR="${SCRIPT_DIR}/patches"
STAMP="${SCRIPT_DIR}/.build_state"

NPROC=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

COLOR_GREEN="\033[0;32m"
COLOR_RED="\033[0;31m"
COLOR_YELLOW="\033[0;33m"
COLOR_OFF="\033[0m"

say()  { echo -e "${COLOR_GREEN}$*${COLOR_OFF}"; }
warn() { echo -e "${COLOR_YELLOW}$*${COLOR_OFF}" >&2; }
die()  { echo -e "${COLOR_RED}$*${COLOR_OFF}" >&2; exit 1; }

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

# --- Build flavor + fingerprint -----------------------------------------

# One string capturing the requested build variant. Baked into the
# fingerprint so switching flavor forces a rebuild.
build_flavor() {
    local io=""
    [ "${AIOPQUIC_IO_URING:-0}" = "1" ] && io="+iouring"
    if [ "${AIOPQUIC_WHEEL_BUILD:-0}" = "1" ]; then
        echo "wheel${io}"
    elif [ "${AIOPQUIC_PERF:-1}" = "1" ]; then
        echo "perf${io}"
    else
        echo "plain${io}"
    fi
}

# Content hash of everything that should invalidate the native build:
# submodule SHAs, applied patches, flavor, arch, compiler.
compute_fingerprint() {
    local pico ptls liburing patches flavor arch cc
    pico=$(git -C "${PICOQUIC_DIR}" rev-parse HEAD 2>/dev/null || echo none)
    ptls=$(git -C "${PICOTLS_DIR}" rev-parse HEAD 2>/dev/null || echo none)
    liburing=$(git -C "${LIBURING_DIR}" rev-parse HEAD 2>/dev/null || echo none)
    if compgen -G "${PATCH_DIR}/*.patch" >/dev/null 2>&1; then
        patches=$(cat "${PATCH_DIR}"/*.patch | _sha256)
    else
        patches=none
    fi
    flavor=$(build_flavor)
    arch=$(uname -m)
    cc=$(${CC:-cc} --version 2>/dev/null | head -1 || echo unknown)
    printf '%s|%s|%s|%s|%s|%s|%s' \
        "${pico}" "${ptls}" "${liburing}" "${patches}" "${flavor}" "${arch}" "${cc}" \
        | _sha256
}

stamp_fingerprint() {
    [ -f "${STAMP}" ] && grep '^FINGERPRINT=' "${STAMP}" 2>/dev/null | cut -d= -f2 || true
}

write_stamp() {
    {
        echo "FINGERPRINT=$(compute_fingerprint)"
        echo "picoquic=$(git -C "${PICOQUIC_DIR}" rev-parse HEAD 2>/dev/null || echo none)"
        echo "picotls=$(git -C "${PICOTLS_DIR}" rev-parse HEAD 2>/dev/null || echo none)"
        echo "flavor=$(build_flavor)"
        echo "arch=$(uname -m)"
        echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
    } > "${STAMP}"
}

artifacts_present() {
    local lib
    lib="$(find "${BUILD_DIR}" -name libpicoquic-core.a -print -quit 2>/dev/null || true)"
    [ -n "${lib}" ]
}

# native compile is needed if forced, or artifacts missing, or no stamp,
# or the stamp's fingerprint no longer matches the current inputs.
need_native() {
    [ "${FORCE:-0}" = "1" ] && return 0
    artifacts_present || return 0
    [ -f "${STAMP}" ] || return 0
    [ "$(stamp_fingerprint)" = "$(compute_fingerprint)" ] || return 0
    return 1
}

# --- Submodule reconcile ------------------------------------------------

# Echo the space-separated list of drifted/uninit submodule paths (empty
# when in sync). liburing uninit is ignored unless io_uring is requested,
# since it is only checked out on demand.
detect_drift() {
    local out="" line flag rest path
    while IFS= read -r line; do
        [ -z "${line}" ] && continue
        flag="${line:0:1}"
        rest="${line:1}"
        # shellcheck disable=SC2086
        set -- ${rest}
        path="${2:-}"
        case "${flag}" in
            '+'|'U') out+="${path} " ;;
            '-')
                if [[ "${path}" == *liburing* ]] && [ "${AIOPQUIC_IO_URING:-0}" != "1" ]; then
                    :
                else
                    out+="${path} "
                fi
                ;;
        esac
    done < <(git -C "${SCRIPT_DIR}" submodule status --recursive 2>/dev/null)
    printf '%s' "${out}"
}

reconcile_submodules() {
    local drift
    drift="$(detect_drift)"
    if [ -n "${drift}" ]; then
        warn "submodule drift (working tree != recorded commit): ${drift}"
        say "reconciling picoquic + picotls to the checked-out commit..."
        git -C "${SCRIPT_DIR}" submodule update --init --recursive \
            third_party/picoquic third_party/picotls
        if [ "${AIOPQUIC_IO_URING:-0}" = "1" ]; then
            git -C "${SCRIPT_DIR}" submodule update --init --recursive third_party/liburing
        fi
    else
        say "submodules in sync"
    fi
}

# --- Extension relink + verify ------------------------------------------

install_extension() {
    say "Relinking Python extension (editable install)..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install -e "${SCRIPT_DIR}"
    else
        pip install -e "${SCRIPT_DIR}"
    fi
}

# Verify the imported aiopquic resolves to THIS source tree (not a
# site-packages wheel) and, when a host-tuned x86_64 build is expected,
# that Fusion AES-GCM is actually linked into the extension. Returns
# nonzero on any mismatch. Prints one status line per check.
verify_imported() {
    local expect_fusion=0 flavor arch
    flavor="$(build_flavor)"
    arch="$(uname -m)"
    if [[ "${flavor}" == perf* ]] && { [ "${arch}" = "x86_64" ] || [ "${arch}" = "amd64" ]; }; then
        expect_fusion=1
    fi
    python - "${SCRIPT_DIR}" "${expect_fusion}" <<'PY'
import sys, os, glob, subprocess
script_dir, expect_fusion = sys.argv[1], sys.argv[2] == "1"
ok = True
try:
    import aiopquic
except Exception as e:
    print("  imported   : FAIL - cannot import aiopquic (%s)" % e)
    sys.exit(3)
d = os.path.dirname(os.path.realpath(aiopquic.__file__))
src = os.path.realpath(os.path.join(script_dir, "src", "aiopquic"))
if d == src:
    print("  imported   : %s  (source tree) OK" % d)
else:
    print("  imported   : %s" % d)
    print("               ^ NOT this source tree (%s)" % src)
    print("               (a portable wheel likely clobbered the editable install)")
    ok = False
if expect_fusion:
    so = glob.glob(d + "/_binding/_transport*.so")
    if not so:
        print("  fusion     : FAIL - no _transport extension found")
        ok = False
    else:
        try:
            syms = subprocess.run(["nm", so[0]], capture_output=True, text=True).stdout
        except FileNotFoundError:
            print("  fusion     : SKIP - 'nm' not available to inspect symbols")
            syms = ""
        if syms:
            if "ptls_fusion_aes128gcm" in syms:
                print("  fusion     : host-tuned OK (Fusion AES-GCM linked)")
            else:
                print("  fusion     : FAIL - Fusion not linked (portable / PERF=0 build)")
                ok = False
sys.exit(0 if ok else 4)
PY
}

# --- Native build (picotls + picoquic) ----------------------------------
# This is the phase build_picoquic.sh delegates to. Logic is preserved
# verbatim from the former build_picoquic.sh so CI / cibuildwheel behave
# identically.
build_native() {
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

    # Host-tuned performance flags. Enabled by DEFAULT for source builds:
    #   - DISABLE_DEBUG_PRINTF (every platform; strips dbg branches)
    #   - PTLS Fusion AES-GCM (x86_64 only; picotls runtime-CPUIDs
    #     AES-NI + PCLMULQDQ so the lib is safe to bake in even on
    #     hosts that lack them — runtime falls back to picotls openssl)
    #   - -O3 -march=native -flto (every platform; host-tuned ISA)
    #
    # Knobs:
    #   AIOPQUIC_PERF=0         — explicit opt-out (debug / unoptimized builds)
    #   AIOPQUIC_WHEEL_BUILD=1  — suppress host-tuned flags entirely so the
    #                             caller-provided PICOQUIC_C_FLAGS /
    #                             CMAKE_ARGS env (e.g. -march=x86-64-v3) drive
    #                             the build. cibuildwheel sets this in
    #                             pyproject.toml so wheels stay portable.
    PICOQUIC_PERF_ARGS=()
    PICOTLS_PERF_ARGS=()
    if [ "${AIOPQUIC_WHEEL_BUILD:-0}" = "1" ]; then
        echo -e "${COLOR_GREEN}AIOPQUIC_WHEEL_BUILD=1: portable build, host-tuned flags suppressed (caller drives PICOQUIC_C_FLAGS / CMAKE_ARGS)${COLOR_OFF}"
    elif [ "${AIOPQUIC_PERF:-1}" = "1" ]; then
        echo -e "${COLOR_GREEN}AIOPQUIC_PERF=1 (default): host-tuned perf flags enabled (Fusion if x86_64, DISABLE_DEBUG_PRINTF, -O3 -march=native -flto)${COLOR_OFF}"
        PICOQUIC_PERF_ARGS+=("-DDISABLE_DEBUG_PRINTF=ON")
        PERF_ARCH="$(uname -m)"
        if [ "${PERF_ARCH}" = "x86_64" ] || [ "${PERF_ARCH}" = "amd64" ]; then
            PICOQUIC_PERF_ARGS+=("-DPTLS_WITH_FUSION=ON")
            PICOTLS_PERF_ARGS+=("-DWITH_FUSION=ON")
            WANT_FUSION=1
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

    # Fusion lives in its own static lib (picotls ADD_LIBRARY(picotls-fusion)).
    # picoquic's FindPTLS only auto-locates it via find_library HINTS that don't
    # cover our out-of-tree picotls build dir, so hand it the explicit path;
    # otherwise picoquic compiles the fusion refs but links nothing that defines
    # ptls_fusion_* (undefined-reference link failure on hosts without a system
    # picotls-fusion).
    if [ "${WANT_FUSION:-0}" = "1" ]; then
        PTLS_FUSION_LIB="${PTLS_BUILD_DIR}/libpicotls-fusion.a"
        if [ ! -f "${PTLS_FUSION_LIB}" ]; then
            echo -e "${COLOR_RED}ERROR: expected ${PTLS_FUSION_LIB} after picotls build (WITH_FUSION=ON)${COLOR_OFF}" >&2
            exit 1
        fi
        PICOQUIC_PERF_ARGS+=("-DPTLS_FUSION_LIBRARY=${PTLS_FUSION_LIB}")
    fi

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
}

# --- Doctor (read-only) -------------------------------------------------

do_check() {
    local issues=0 drift
    say "aiopquic build doctor (read-only)"

    drift="$(detect_drift)"
    if [ -n "${drift}" ]; then
        echo -e "  submodules : ${COLOR_RED}DRIFT${COLOR_OFF} (${drift% })"
        issues=1
    else
        echo -e "  submodules : ${COLOR_GREEN}in sync${COLOR_OFF}"
    fi

    if ! artifacts_present; then
        echo -e "  native     : ${COLOR_RED}NOT BUILT${COLOR_OFF}"
        issues=1
    elif [ ! -f "${STAMP}" ]; then
        echo -e "  native     : ${COLOR_YELLOW}UNKNOWN (no .build_state stamp)${COLOR_OFF}"
        issues=1
    elif [ "$(stamp_fingerprint)" != "$(compute_fingerprint)" ]; then
        echo -e "  native     : ${COLOR_RED}STALE (inputs changed since last build)${COLOR_OFF}"
        issues=1
    else
        echo -e "  native     : ${COLOR_GREEN}up to date${COLOR_OFF} (flavor $(build_flavor))"
    fi

    if verify_imported; then
        :
    else
        issues=1
    fi

    if [ "${issues}" = "0" ]; then
        say "OK — installed + imported aiopquic reflects the current source"
        return 0
    fi
    warn "issues found — run ./build.sh to reconcile"
    return 1
}

# --- Full (default) -----------------------------------------------------

do_full() {
    reconcile_submodules
    if need_native; then
        say "native libs stale or missing — compiling..."
        build_native
        write_stamp
        install_extension
    else
        say "native libs up to date (fingerprint match) — skipping compile"
    fi
    # Guarantee imported == source even when native was already fresh —
    # this is what catches a wheel that clobbered the editable install.
    if ! verify_imported; then
        warn "imported aiopquic does not reflect this source tree — relinking..."
        install_extension
        verify_imported || die "verification still failing after relink"
    fi
    say "OK — installed + imported aiopquic reflects the current source"
}

# --- Arg parse ----------------------------------------------------------

MODE=full
FORCE=0

usage() {
    sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check)   MODE=check ;;
        --native)  MODE=native ;;
        --install) MODE=install ;;
        --verify)  MODE=verify ;;
        --force)   FORCE=1 ;;
        -h|--help|-\?) usage 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

case "${MODE}" in
    check)   do_check ;;
    native)  build_native; write_stamp ;;
    install) install_extension; verify_imported ;;
    verify)  verify_imported ;;
    full)    do_full ;;
esac
