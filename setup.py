"""Build aiopquic Cython extensions linking against pre-built picoquic + picotls."""

import os
import platform
import sys
from setuptools import Extension, setup
from Cython.Build import cythonize

ROOT = os.path.dirname(os.path.abspath(__file__))

PICOQUIC_DIR = os.path.join(ROOT, "third_party", "picoquic")
PICOTLS_DIR = os.path.join(ROOT, "third_party", "picotls")
PICOQUIC_BUILD = os.path.join(PICOQUIC_DIR, "build")
PICOQUIC_INC = os.path.join(PICOQUIC_DIR, "picoquic")
PICOHTTP_INC = os.path.join(PICOQUIC_DIR, "picohttp")
PICOTLS_INC = os.path.join(PICOTLS_DIR, "include")
PICOTLS_BUILD = os.path.join(PICOQUIC_BUILD, "picotls-build")


def find_lib(name, search_dirs):
    for d in search_dirs:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


PICOQUIC_LIB_DIRS = [PICOQUIC_BUILD, os.path.join(PICOQUIC_BUILD, "picoquic")]
PTLS_LIB_DIRS = [PICOTLS_BUILD]

# Skip the picoquic-built check for commands that don't actually compile
# the extension. sdist + egg_info + dist_info just package source files
# and don't need the static libraries; failing them would block source
# distribution from a fresh checkout that hasn't run build_picoquic.sh.
_NO_BUILD_CMDS = {
    "sdist", "egg_info", "dist_info", "check",
    "--help", "--help-commands", "--name", "--version",
    "--fullname", "--author", "--description", "--long-description",
}
_needs_libs = not _NO_BUILD_CMDS.intersection(sys.argv[1:])

picoquic_lib = find_lib("libpicoquic-core.a", PICOQUIC_LIB_DIRS)
if picoquic_lib is None and _needs_libs:
    print("ERROR: picoquic not built. Run: ./build_picoquic.sh", file=sys.stderr)
    sys.exit(1)

# picohttp-core: H3 + WebTransport + h3zero. Provides picowt_*,
# h3zero_*, picohttp_* symbols. Must precede picoquic-core in the
# link line (it calls into core).
extra_objects = []
http_lib = find_lib("libpicohttp-core.a", PICOQUIC_LIB_DIRS)
if http_lib is not None:
    extra_objects.append(http_lib)
if picoquic_lib is not None:
    extra_objects.append(picoquic_lib)
# picoquic-log is split out of picoquic-core in upstream; provides
# picoquic_set_qlog/picoquic_set_textlog/etc. Must follow core in
# the link line.
log_lib = find_lib("libpicoquic-log.a", PICOQUIC_LIB_DIRS)
if log_lib is not None:
    extra_objects.append(log_lib)

for lib_name in ("libpicotls-core.a", "libpicotls-openssl.a",
                 "libpicotls-minicrypto.a"):
    p = find_lib(lib_name, PTLS_LIB_DIRS)
    if p is not None:
        extra_objects.append(p)
# Optional/architecture-specific picotls backends — link if present.
for lib_name in ("libpicotls-fusion.a",):
    p = find_lib(lib_name, PTLS_LIB_DIRS)
    if p is not None:
        extra_objects.append(p)

# io_uring: when AIOPQUIC_IO_URING=1 was set at picoquic-build time,
# libpicoquic-core.a has undefined references to io_uring_* symbols
# that need liburing.a in the final link. Search CMAKE_PREFIX_PATH
# (same env var used for the cmake side) and the vendored install
# under third_party/liburing/install.
if os.environ.get("AIOPQUIC_IO_URING", "0") == "1" and platform.system() == "Linux":
    _uring_search = [
        os.path.join(p, "lib")
        for p in os.environ.get("CMAKE_PREFIX_PATH", "").split(":") if p
    ] + [os.path.join(ROOT, "third_party", "liburing", "install", "lib")]
    _uring_lib = find_lib("liburing.a", _uring_search)
    if _uring_lib is not None:
        extra_objects.append(_uring_lib)
        print(f"io_uring: linking {_uring_lib}")
    elif _needs_libs:
        print("WARNING: AIOPQUIC_IO_URING=1 but liburing.a not found in "
              f"CMAKE_PREFIX_PATH or {ROOT}/third_party/liburing/install/lib",
              file=sys.stderr)

def _detect_brew_openssl():
    """On macOS, fall back to Homebrew's openssl@3 / openssl@1.1 prefix
    when OPENSSL_ROOT_DIR isn't set. Mirrors the equivalent detection
    in build_picoquic.sh so the final-extension link line knows where
    -lssl / -lcrypto live."""
    if platform.system() != "Darwin":
        return None
    import shutil
    import subprocess
    if not shutil.which("brew"):
        return None
    for pkg in ("openssl@3", "openssl@1.1"):
        try:
            prefix = subprocess.check_output(
                ["brew", "--prefix", pkg], text=True
            ).strip()
        except Exception:
            continue
        if prefix and os.path.isdir(prefix):
            return prefix
    return None


# OpenSSL discovery: explicit env var first (CI / advanced users),
# then Homebrew on macOS, else system include/lib paths.
openssl_root = os.environ.get("OPENSSL_ROOT_DIR") or _detect_brew_openssl()
include_dirs = [
    os.path.join(ROOT, "src", "aiopquic", "_binding"),
    PICOQUIC_INC,
    PICOHTTP_INC,
    PICOTLS_INC,
]
library_dirs = []
if openssl_root:
    include_dirs.append(os.path.join(openssl_root, "include"))
    for libdir in ("lib", "lib64"):
        candidate = os.path.join(openssl_root, libdir)
        if os.path.isdir(candidate):
            library_dirs.append(candidate)

# picoquic-core and picoquic-log have a circular dependency (log
# calls into core's frame helpers; core has hooks into log's
# qlog/textlog). On GNU ld, --start-group/--end-group force a rescan
# of the static archives until all references resolve. Apple ld and
# lld rescan archives by default so the flags are unnecessary (and
# Apple ld outright rejects them).
extra_link_args = []
if platform.system() == "Linux":
    # Order matters: setuptools places `libraries=` BEFORE
    # `extra_link_args=` on the cc line. The static archives in the
    # group have undefined references to EVP_*/SSL_* — those need
    # libcrypto/libssl to come AFTER the group, not before. Move
    # -lssl/-lcrypto into extra_link_args here so the resulting
    # order is: ... -Wl,--start-group [archives] -Wl,--end-group
    # -lssl -lcrypto -lpthread.
    extra_link_args = [
        "-Wl,--start-group",
        *extra_objects,
        "-Wl,--end-group",
        "-lssl",
        "-lcrypto",
        "-lpthread",
    ]
    extra_objects_for_ext = []
    libraries = []
else:
    extra_objects_for_ext = extra_objects
    # Platform link libs. pthread is implicit on macOS but Apple ld
    # warns/errors on -lpthread in some toolchains, so omit it there.
    libraries = ["ssl", "crypto"]
    if platform.system() != "Darwin":
        libraries.append("pthread")

define_macros = []
if platform.system() == "Linux":
    define_macros.append(("_GNU_SOURCE", "1"))

# Must mirror the picoquic build-time -DPICOQUIC_WITH_IO_URING.
# picoquic_network_thread_ctx_t and picoquic_socket_ctx_t have
# conditional fields (pipe_iovec, is_pipe_io_uring_started, msghdr
# msg, ctrl_buffer, …) — a mismatch silently shifts every later
# field's offset (incl. thread_is_ready) and the network thread
# appears to never become ready.
if os.environ.get("AIOPQUIC_IO_URING", "0") == "1" and platform.system() == "Linux":
    define_macros.append(("PICOQUIC_WITH_IO_URING", "1"))

print(f"Linking against: {extra_objects}")
if library_dirs:
    print(f"Library dirs: {library_dirs}")

extensions = [
    Extension(
        "aiopquic._binding._transport",
        sources=[os.path.join("src", "aiopquic", "_binding", "_transport.pyx")],
        include_dirs=include_dirs,
        library_dirs=library_dirs,
        extra_objects=extra_objects_for_ext,
        extra_link_args=extra_link_args,
        libraries=libraries,
        language="c",
        define_macros=define_macros,
    ),
    # StreamChain — pure-Cython, no picoquic deps. Used by aiomoqt's
    # parser as the per-stream byte accumulator. Ships in aiopquic so
    # one native build covers both packages; aiomoqt imports it as
    # `from aiopquic.streamchain import StreamChain`.
    Extension(
        "aiopquic._binding._streamchain",
        sources=[os.path.join(
            "src", "aiopquic", "_binding", "_streamchain.pyx")],
        language="c",
    ),
    Extension(
        "aiopquic._binding._buffer",
        sources=[os.path.join(
            "src", "aiopquic", "_binding", "_buffer.pyx")],
        language="c",
    ),
]


# Capture picoquic + picotls submodule revisions at build time. Both
# are vendored as git submodules; their HEAD info travels with the
# built wheel via the generated src/aiopquic/_build_info.py module.
# `python -m aiopquic.versions` reads this file. If git isn't
# available (e.g. building from an sdist that didn't ship .git),
# every field degrades to "unknown".
#
# For each submodule we capture:
#   SHA       - full 40-char HEAD commit hash
#   DESCRIBE  - `git describe --tags --always --long` (vN.M.P-K-gHASH)
#               telling you which upstream release HEAD is closest to
#   DATE      - commit author-date in ISO-8601 (YYYY-MM-DD HH:MM:SS ±TZ)
#   SUBJECT   - commit subject line (first 80 chars)
#   BRANCH    - upstream default branch the pinned commit sits on
# For picoquic we also capture PICOQUIC_VERSION (the library version
# string from picoquic.h) — a human-readable release vs. the raw SHA.
def _git_info(path):
    if not os.path.exists(os.path.join(path, ".git")):
        return {"sha": "unknown", "describe": "unknown", "date": "unknown",
                "subject": "unknown", "branch": "unknown"}
    import subprocess
    def _run(args):
        try:
            return subprocess.check_output(
                ["git", "-C", path] + args,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return "unknown"
    subject = _run(["log", "-1", "--pretty=%s", "HEAD"])
    if subject != "unknown" and len(subject) > 80:
        subject = subject[:77] + "..."
    # Submodules sit at a detached pinned commit; name the upstream
    # default branch (origin/HEAD) when the pinned commit is on it.
    branch = "unknown"
    head = _run(["rev-parse", "HEAD"])
    ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"])
    if head != "unknown" and "/" in ref:
        cand = ref.rsplit("/", 1)[-1]
        on_branch = subprocess.call(
            ["git", "-C", path, "merge-base", "--is-ancestor",
             head, "origin/" + cand],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) == 0
        if on_branch:
            branch = cand
    return {
        "sha":      _run(["rev-parse", "HEAD"]),
        "describe": _run(["describe", "--tags", "--always", "--long"]),
        "date":     _run(["log", "-1", "--pretty=%ci", "HEAD"]),
        "subject":  subject,
        "branch":   branch,
    }


def _picoquic_version(path):
    """Parse PICOQUIC_VERSION "X.Y.Z" out of picoquic.h, or "unknown"."""
    import re
    header = os.path.join(path, "picoquic", "picoquic.h")
    try:
        with open(header) as _h:
            text = _h.read()
    except OSError:
        return "unknown"
    m = re.search(r'#\s*define\s+PICOQUIC_VERSION\s+"([^"]+)"', text)
    return m.group(1) if m else "unknown"


_pq_info  = _git_info(PICOQUIC_DIR)
_ptls_info = _git_info(PICOTLS_DIR)
_pq_version = _picoquic_version(PICOQUIC_DIR)

_build_info_path = os.path.join(ROOT, "src", "aiopquic", "_build_info.py")
with open(_build_info_path, "w") as _f:
    _f.write(
        '"""Build-time submodule revisions. Auto-generated by setup.py."""\n'
        f'PICOQUIC_VERSION  = {_pq_version!r}\n'
        f'PICOQUIC_SHA      = {_pq_info["sha"]!r}\n'
        f'PICOQUIC_DESCRIBE = {_pq_info["describe"]!r}\n'
        f'PICOQUIC_DATE     = {_pq_info["date"]!r}\n'
        f'PICOQUIC_SUBJECT  = {_pq_info["subject"]!r}\n'
        f'PICOQUIC_BRANCH   = {_pq_info["branch"]!r}\n'
        f'PICOTLS_SHA       = {_ptls_info["sha"]!r}\n'
        f'PICOTLS_DESCRIBE  = {_ptls_info["describe"]!r}\n'
        f'PICOTLS_DATE      = {_ptls_info["date"]!r}\n'
        f'PICOTLS_SUBJECT   = {_ptls_info["subject"]!r}\n'
        f'PICOTLS_BRANCH    = {_ptls_info["branch"]!r}\n'
    )


setup(
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    ),
)
