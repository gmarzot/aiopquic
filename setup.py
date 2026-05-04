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

# OpenSSL discovery: env var (CI / Homebrew) wins, else system.
openssl_root = os.environ.get("OPENSSL_ROOT_DIR")
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
