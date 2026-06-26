#!/usr/bin/env bash
# Update vendored submodules (picoquic / picotls / liburing) to the latest
# commit that still builds and passes the aiopquic test suite.
#
# The "pin" is the submodule SHA recorded in this superproject. picoquic
# and picotls do not cut release tags we track, so "latest release" means
# "newest commit on the upstream tracking branch". liburing is pinned to a
# release tag, so its candidates are version tags.
#
# Modes:
#   (default)        Dry-run: fetch upstream and report drift only. No
#                    checkout, no build, no changes.
#   --advance        For each selected submodule, try the newest upstream
#                    commit (or --to REF): check it out, rebuild, run the
#                    gate. On pass, stage that pin. On FAILURE, restore the
#                    original pin and stop (fail-fast) — the gate output and
#                    the captured pip-install log show why.
#
# Options:
#   --only NAME      Restrict to one submodule (picoquic|picotls|liburing).
#                    Repeatable.
#   --to REF         Only meaningful with --only: try exactly this ref
#                    (commit/tag/branch) instead of upstream newest.
#   --walk           If the newest commit fails the gate, step back through
#                    older commits (newest -> oldest) until one passes
#                    ("latest passing"). OFF by default — each step is a
#                    full rebuild + test, so an API break grinds for a long
#                    time. Use only when a single commit is expected to fail.
#   --force          When a submodule is already at its target, re-run the
#                    build+test gate at the current pin anyway (verify it
#                    still builds/passes). Without it, an already-current pin
#                    is a no-op (no recompile).
#   --no-build       Skip rebuild + gate; just move the pin (with --advance).
#   --commit         After a successful --advance, commit the staged pin
#                    update(s) with a generated message.
#
# Env:
#   AIOPQUIC_SKIP_PATCHES   honored by build_picoquic.sh during the gate.
#   PYTEST_ARGS             extra args appended to the pytest gate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

# pip-install (extension relink) output is captured here so a link/ABI
# failure is shown instead of silently swallowed.
GATE_LOG="${SCRIPT_DIR}/.update_submodules_gate.log"

COLOR_GREEN="\033[0;32m"
COLOR_RED="\033[0;31m"
COLOR_YELLOW="\033[0;33m"
COLOR_BOLD="\033[1m"
COLOR_OFF="\033[0m"

say()  { echo -e "${COLOR_GREEN}$*${COLOR_OFF}"; }
warn() { echo -e "${COLOR_YELLOW}$*${COLOR_OFF}" >&2; }
die()  { echo -e "${COLOR_RED}$*${COLOR_OFF}" >&2; exit 1; }

# --- Submodule registry --------------------------------------------------
# Pin model per submodule:
#   picoquic  driver  — tracks upstream master HEAD (newest commit).
#   picotls   derived — NOT its own master. Target = the commit picoquic
#                       blesses (PICOQUIC_FETCH_PTLS_DEFAULT_TAG in picoquic's
#                       CMakeLists, non-AEGIS branch; cross-checked against
#                       ci/build_picotls.sh). Follows picoquic.
#   liburing  tag/held — aiopquic's own io_uring dep. picoquic only
#                       find_library()s it, blessing no version, so there is
#                       nothing to derive from. Pinned to a known-good and
#                       HELD: never auto-advanced (a newer tag may build but
#                       break the io_uring runtime). Advance only deliberately
#                       via --only liburing; tag-mode picks the newest stable
#                       tag as the candidate when you do.
declare -A SUB_PATH=(
    [picoquic]="third_party/picoquic"
    [picotls]="third_party/picotls"
    [liburing]="third_party/liburing"
)
declare -A SUB_BRANCH=(
    [picoquic]="master"
    [picotls]="master"
    [liburing]="master"
)
declare -A SUB_TAGMODE=(
    [picoquic]=0
    [picotls]=0
    [liburing]=1
)
# Derived pins take their target from another component, not upstream.
declare -A SUB_DERIVED=(
    [picoquic]=0
    [picotls]=1
    [liburing]=0
)
# Held pins are pinned to a known-good and skipped by the default --advance
# sweep; advance them only when named explicitly via --only.
declare -A SUB_HELD=(
    [picoquic]=0
    [picotls]=0
    [liburing]=1
)
ALL_SUBS=(picoquic picotls liburing)

# --- Parse args ---
MODE="dryrun"
NO_BUILD=0
DO_COMMIT=0
WALK=0
FORCE=0
EXPLICIT_SELECT=0
TO_REF=""
SELECTED=()

while [ $# -gt 0 ]; do
    case "$1" in
        --advance)  MODE="advance" ;;
        --dry-run)  MODE="dryrun" ;;
        --walk)     WALK=1 ;;
        --force)    FORCE=1 ;;
        --no-build) NO_BUILD=1 ;;
        --commit)   DO_COMMIT=1 ;;
        --only)     shift; [ $# -gt 0 ] || die "--only needs a submodule name"; SELECTED+=("$1"); EXPLICIT_SELECT=1 ;;
        --to)       shift; [ $# -gt 0 ] || die "--to needs a ref"; TO_REF="$1" ;;
        -h|--help)  sed -n '2,48p' "$0"; exit 0 ;;
        *)          die "unknown option: $1" ;;
    esac
    shift
done

if [ ${#SELECTED[@]} -eq 0 ]; then
    SELECTED=("${ALL_SUBS[@]}")
fi
for name in "${SELECTED[@]}"; do
    [ -n "${SUB_PATH[$name]:-}" ] || die "unknown submodule: ${name}"
done
if [ -n "${TO_REF}" ] && [ ${#SELECTED[@]} -ne 1 ]; then
    die "--to requires exactly one --only <submodule>"
fi

# --- Helpers ------------------------------------------------------------

short() { git -C "$1" rev-parse --short "$2" 2>/dev/null || echo "?"; }

# Relationship of a pin to its upstream/target ref, as a signed status:
#   in-sync | behind: N | ahead: N | ahead: N behind: M (diverged).
# "behind" = commits upstream has that we don't; "ahead" = vice-versa.
sync_status() {
    local path="$1" pin="$2" up="$3"
    local lr ahead behind
    lr="$(git -C "${path}" rev-list --left-right --count "${pin}...${up}" 2>/dev/null)" || { echo "?"; return; }
    ahead="${lr%%[[:space:]]*}"; behind="${lr##*[[:space:]]}"
    if   [ "${ahead}" = "0" ] && [ "${behind}" = "0" ]; then echo "in-sync"
    elif [ "${ahead}" = "0" ]; then echo "behind: ${behind}"
    elif [ "${behind}" = "0" ]; then echo "ahead: ${ahead}"
    else echo "ahead: ${ahead} behind: ${behind}"
    fi
}

# Version string for a submodule at a given ref (best-effort, human info).
version_at() {
    local name="$1" path="$2" ref="$3"
    case "${name}" in
        picoquic)
            git -C "${path}" show "${ref}:picoquic/picoquic.h" 2>/dev/null \
                | sed -n 's/.*PICOQUIC_VERSION "\([^"]*\)".*/\1/p' | head -1 ;;
        liburing)
            git -C "${path}" describe --tags "${ref}" 2>/dev/null ;;
        *)
            git -C "${path}" show -s --format=%cs "${ref}" 2>/dev/null ;;  # commit date
    esac
}

# The picotls commit picoquic (at its currently checked-out tree) blesses.
# Primary source: PICOQUIC_FETCH_PTLS_DEFAULT_TAG in picoquic's CMakeLists,
# else-branch (WITH_AEGIS OFF — our build). Cross-checked against the CI
# helper ci/build_picotls.sh; warns if the two disagree. Echoes the 40-hex.
picoquic_blessed_picotls() {
    local pq="${SUB_PATH[picoquic]}"
    local cmake_tag ci_tag
    cmake_tag="$(awk '
        /if *\(WITH_AEGIS\)/      {blk=1; branch="if"; next}
        blk && /else *\( *\)/     {branch="else"; next}
        blk && /endif *\( *\)/    {blk=0; next}
        blk && branch=="else" && /PICOQUIC_FETCH_PTLS_DEFAULT_TAG/ {
            if (match($0, /[0-9a-f]{40}/)) { print substr($0, RSTART, RLENGTH); exit }
        }' "${pq}/CMakeLists.txt" 2>/dev/null)"
    ci_tag="$(grep -oE 'COMMIT_ID=[0-9a-f]{40}' "${pq}/ci/build_picotls.sh" 2>/dev/null | cut -d= -f2 | head -1)"
    if [ -n "${cmake_tag}" ] && [ -n "${ci_tag}" ] && [ "${cmake_tag}" != "${ci_tag}" ]; then
        warn "  picoquic picotls sources DISAGREE: CMake=${cmake_tag:0:12} ci=${ci_tag:0:12} — using CMake"
    fi
    echo "${cmake_tag:-${ci_tag}}"
}

# Echo candidate refs newest -> oldest for a submodule (one SHA per line).
candidates() {
    local name="$1" path="$2"
    local pin; pin="$(git -C "${path}" rev-parse HEAD)"
    if [ -n "${TO_REF}" ]; then
        git -C "${path}" rev-parse "${TO_REF}^{commit}"
        return
    fi
    if [ "${SUB_DERIVED[$name]:-0}" = "1" ]; then
        # single target: whatever the current picoquic blesses
        local blessed; blessed="$(picoquic_blessed_picotls)"
        [ -n "${blessed}" ] || { warn "  could not derive blessed ${name} from picoquic"; return; }
        local bsha; bsha="$(git -C "${path}" rev-parse "${blessed}^{commit}" 2>/dev/null)" \
            || { warn "  blessed ${name} ${blessed:0:12} not in local objects (fetch needed)"; return; }
        [ "${bsha}" = "${pin}" ] || echo "${bsha}"   # empty => already in lockstep
        return
    fi
    if [ "${SUB_TAGMODE[$name]}" = "1" ]; then
        # newest version tags above the current pin, newest first
        local cur_tag; cur_tag="$(git -C "${path}" describe --tags --abbrev=0 HEAD 2>/dev/null || true)"
        { git -C "${path}" tag --sort=-version:refname \
            | grep -E '[0-9]+\.[0-9]' \
            | grep -viE 'rc|alpha|beta|pre|dev' \
            | while read -r t; do
                  local sha; sha="$(git -C "${path}" rev-parse "${t}^{commit}")"
                  [ "${sha}" = "${pin}" ] && break
                  echo "${sha}"
              done; } || true
    else
        local branch="${SUB_BRANCH[$name]}"
        # commits on the tracking branch ahead of the pin, newest first
        git -C "${path}" rev-list --first-parent "${pin}..origin/${branch}"
    fi
}

# Run the build + test gate against the currently checked-out submodules.
# Returns 0 on pass, 1 on build failure, 2 on test failure.
run_gate() {
    if [ "${NO_BUILD}" = "1" ]; then
        warn "  --no-build: skipping rebuild + gate"
        return 0
    fi
    say "  building (build_picoquic.sh)..."
    if ! ./build_picoquic.sh; then
        return 1
    fi
    say "  relinking extension (pip install -e .)..."
    if ! pip install -e . >"${GATE_LOG}" 2>&1; then
        warn "  pip install FAILED (likely a picotls/picoquic API change) — tail of ${GATE_LOG}:"
        tail -20 "${GATE_LOG}" >&2
        return 1
    fi
    say "  testing (pytest -m 'not interop' -x — fail-fast)..."
    if ! pytest -m "not interop" -x -n auto ${PYTEST_ARGS:-}; then
        return 2
    fi
    return 0
}

# --- Status table -------------------------------------------------------

# Print the submodule version-status table (no footer). Shared by the
# dry-run report and the --advance overview.
status_table() {
    printf '%-10s  %-12s  %-12s  %-18s  %s\n' "submodule" "pin" "upstream" "status" "version (pin -> upstream)"
    for name in "${SELECTED[@]}"; do
        local path="${SUB_PATH[$name]}"
        [ -d "${path}/.git" ] || [ -f "${path}/.git" ] || { warn "${name}: submodule not initialized"; continue; }
        git -C "${path}" fetch --tags -q origin 2>/dev/null || warn "${name}: fetch failed"
        local pin upstream status
        pin="$(git -C "${path}" rev-parse HEAD)"
        if [ "${SUB_DERIVED[$name]:-0}" = "1" ]; then
            local blessed; blessed="$(picoquic_blessed_picotls)"
            upstream="$(git -C "${path}" rev-parse "${blessed}^{commit}" 2>/dev/null || echo "${pin}")"
        elif [ "${SUB_TAGMODE[$name]}" = "1" ]; then
            local newest_tag
            newest_tag="$(git -C "${path}" tag --sort=-version:refname \
                | grep -E '[0-9]+\.[0-9]' | grep -viE 'rc|alpha|beta|pre|dev' | head -1 || true)"
            upstream="$(git -C "${path}" rev-parse "${newest_tag:-HEAD}^{commit}" 2>/dev/null || echo "${pin}")"
        else
            upstream="$(git -C "${path}" rev-parse "origin/${SUB_BRANCH[$name]}")"
        fi
        status="$(sync_status "${path}" "${pin}" "${upstream}")"
        local vp vu
        vp="$(version_at "${name}" "${path}" "${pin}")"
        vu="$(version_at "${name}" "${path}" "${upstream}")"
        printf '%-10s  %-12s  %-12s  %-18s  %s -> %s\n' \
            "${name}" "$(short "${path}" "${pin}")" "$(short "${path}" "${upstream}")" \
            "${status}" "${vp:-?}" "${vu:-?}"
    done
    # Footnotes: held pins are pinned locally and skipped by default.
    for name in "${SELECTED[@]}"; do
        [ "${SUB_HELD[$name]:-0}" = "1" ] \
            && warn "${name}: pinned locally (use --only ${name} to attempt update)"
    done
}

# --- Dry-run report -----------------------------------------------------

dry_report() {
    status_table
    say "dry-run only. re-run with --advance to update submodules (gated by build+pytest)."
}

# --- Advance ---------------------------------------------------------------

advance_one() {
    local name="$1" path="$2"
    local orig; orig="$(git -C "${path}" rev-parse HEAD)"

    git -C "${path}" fetch --tags -q origin 2>/dev/null || warn "${name}: fetch failed"

    local cands; cands="$(candidates "${name}" "${path}")"
    if [ -z "${cands}" ]; then
        # Already at target — the status table already showed "in-sync";
        # stay silent unless --force asks to re-verify the gate.
        if [ "${FORCE}" = "1" ]; then
            say "${COLOR_BOLD}${name}${COLOR_OFF}: --force — re-running gate at current pin $(short "${path}" "${orig}")"
            local rc=0; run_gate || rc=$?
            [ "${rc}" = "0" ] && say "  gate PASS at current pin" || warn "  gate FAILED (rc=${rc}) at current pin"
            return "${rc}"
        fi
        return 0
    fi
    say "${COLOR_BOLD}${name}${COLOR_OFF}: pin $(short "${path}" "${orig}") ($(version_at "${name}" "${path}" "${orig}")) — updating"
    # Fail-fast default: try only the newest candidate. --walk steps back
    # through older commits (each a full rebuild + test) until one passes.
    if [ "${WALK}" != "1" ]; then
        cands="$(echo "${cands}" | head -1)"
    else
        local n; n="$(echo "${cands}" | wc -l | tr -d ' ')"
        say "  --walk: up to ${n} candidate(s), newest first"
    fi

    local cand
    while read -r cand; do
        [ -n "${cand}" ] || continue
        say "  trying $(short "${path}" "${cand}") ($(version_at "${name}" "${path}" "${cand}"))"
        git -C "${path}" checkout -q "${cand}"
        local rc=0
        run_gate || rc=$?
        if [ "${rc}" = "0" ]; then
            git add "${path}"
            say "  PASS -> staged ${name} at $(short "${path}" "${cand}")"
            ADVANCED+=("${name} updated -> $(short "${path}" "${cand}")")
            return 0
        fi
        local step; [ "${WALK}" = "1" ] && step=" — stepping back" || step=""
        [ "${rc}" = "1" ] \
            && warn "  build/link FAILED at $(short "${path}" "${cand}")${step}" \
            || warn "  tests FAILED at $(short "${path}" "${cand}")${step}"
    done <<< "${cands}"

    warn "  gate failed; reverting ${name} to pin $(short "${path}" "${orig}")"
    git -C "${path}" checkout -q "${orig}"
    if [ "${NO_BUILD}" != "1" ]; then
        say "  rebuilding at original pin..."
        ./build_picoquic.sh >/dev/null 2>&1 || true
        pip install -e . >/dev/null 2>&1 || true
    fi
    [ "${WALK}" = "1" ] || warn "  newest commit failed; re-run with --walk to search older commits for one that passes"
    return 1
}

# --- Main ---------------------------------------------------------------

if [ "${MODE}" = "dryrun" ]; then
    dry_report
    exit 0
fi

# advance mode: same status overview first, then act only on drifted ones.
status_table

ADVANCED=()
fail=0
for name in "${SELECTED[@]}"; do
    # held pins are skipped by the default sweep (the status footnote already
    # said so); advance them only when named explicitly via --only.
    if [ "${SUB_HELD[$name]:-0}" = "1" ] && [ "${EXPLICIT_SELECT}" != "1" ]; then
        continue
    fi
    advance_one "${name}" "${SUB_PATH[$name]}" || fail=1
done

if [ ${#ADVANCED[@]} -eq 0 ]; then
    say "no submodule updates (use --force to force recompile on current versions)."
    exit "${fail}"
fi

joined="$(printf '%s, ' "${ADVANCED[@]}")"
say "${joined%, }"

if [ "${DO_COMMIT}" = "1" ]; then
    msg="build: update vendored submodules"$'\n'
    for b in "${ADVANCED[@]}"; do msg+=$'\n'"  ${b}"; done
    git commit -m "${msg}"
    say "committed."
else
    echo
    say "staged but not committed. review with 'git diff --cached', then commit (or re-run with --commit)."
fi

exit "${fail}"
