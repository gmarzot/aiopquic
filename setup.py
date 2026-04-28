"""Build aiopquic Cython extensions linking against pre-built picoquic."""

import os
import sys
from setuptools import setup, Extension
from Cython.Build import cythonize

ROOT = os.path.dirname(os.path.abspath(__file__))

# picoquic build output location
PICOQUIC_DIR = os.path.join(ROOT, "third_party", "picoquic")
PICOQUIC_BUILD = os.path.join(PICOQUIC_DIR, "build")
PICOQUIC_INC = os.path.join(PICOQUIC_DIR, "picoquic")
PICOHTTP_INC = os.path.join(PICOQUIC_DIR, "picohttp")

# picotls headers
LOCAL_PICOQUIC = "/home/gmarzot/Projects/moq/picoquic"
PICOTLS_INC = os.path.join(LOCAL_PICOQUIC, "_deps", "picotls-src", "include")
PICOTLS_BUILD = os.path.join(PICOQUIC_BUILD, "picotls-build")

# Check that picoquic has been built
picoquic_lib = None
for candidate in [
    os.path.join(PICOQUIC_BUILD, "libpicoquic-core.a"),
    os.path.join(PICOQUIC_BUILD, "picoquic", "libpicoquic-core.a"),
]:
    if os.path.exists(candidate):
        picoquic_lib = candidate
        break

if picoquic_lib is None:
    print("ERROR: picoquic not built. Run: ./build_picoquic.sh", file=sys.stderr)
    sys.exit(1)

# Collect static libraries
extra_objects = [picoquic_lib]

# picohttp-core: H3 + WebTransport + h3zero. Provides picowt_*,
# h3zero_*, picohttp_* symbols. Must precede picoquic-core in the
# link line (it calls into core).
http_lib = os.path.join(PICOQUIC_BUILD, "libpicohttp-core.a")
if os.path.exists(http_lib):
    extra_objects.insert(0, http_lib)

# picoquic-log is split out of picoquic-core in upstream; it provides
# picoquic_set_qlog/picoquic_set_textlog/etc. Must follow picoquic-core
# in the link line (core has undefined references into log).
log_lib = os.path.join(PICOQUIC_BUILD, "libpicoquic-log.a")
if os.path.exists(log_lib):
    extra_objects.append(log_lib)

for lib_name in ["libpicotls-core.a", "libpicotls-openssl.a",
                 "libpicotls-fusion.a", "libpicotls-minicrypto.a"]:
    for search_dir in [PICOTLS_BUILD,
                       os.path.join(PICOQUIC_BUILD, "_deps", "picotls-build")]:
        lib_path = os.path.join(search_dir, lib_name)
        if os.path.exists(lib_path):
            extra_objects.append(lib_path)
            break

print(f"Linking against: {extra_objects}")

# Use RELATIVE path for source — setuptools requires this
# picoquic-core and picoquic-log have a circular dependency (log calls
# into core's frame helpers; core has hooks into log's qlog/textlog).
# Wrap the static archives in --start-group/--end-group so the linker
# rescans them until all undefined references are resolved.
# Dynamic deps (ssl/crypto/pthread) come AFTER the group so the
# group's openssl references can be satisfied.
extra_link_args = [
    "-Wl,--start-group",
    *extra_objects,
    "-Wl,--end-group",
    "-lssl",
    "-lcrypto",
    "-lpthread",
]

extensions = [
    Extension(
        "aiopquic._binding._transport",
        sources=[os.path.join("src", "aiopquic", "_binding", "_transport.pyx")],
        include_dirs=[
            os.path.join(ROOT, "src", "aiopquic", "_binding"),
            PICOQUIC_INC,
            PICOHTTP_INC,
            PICOTLS_INC,
        ],
        extra_link_args=extra_link_args,
        language="c",
        define_macros=[("_GNU_SOURCE", "1")],
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
