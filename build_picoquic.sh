#!/usr/bin/env bash
# Compatibility shim.
#
# The native-compile logic now lives in build.sh. This script builds
# only the native libraries (picotls + picoquic + test drivers) —
# identical to its former behavior — by delegating to
# `build.sh --native`.
#
# Existing callers (CI, cibuildwheel, update_submodules.sh, sim_link)
# need no changes: env (AIOPQUIC_PERF, AIOPQUIC_WHEEL_BUILD,
# AIOPQUIC_IO_URING, AIOPQUIC_SKIP_PATCHES, PICOQUIC_C_FLAGS, ...) and
# args pass through unchanged.
#
# For the full reconcile → build → relink → verify flow, run ./build.sh.
exec "$(dirname "$0")/build.sh" --native "$@"
