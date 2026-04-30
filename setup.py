"""Build aiopquic Cython extensions linking against pre-built picoquic + picotls."""

import os
import platform
import sys
from setuptools import setup, Extension
from Cython.Build import cythonize

ROOT = os.path.dirname(os.path.abspath(__file__))

PICOQUIC_DIR = os.path.join(ROOT, "third_party", "picoquic")
PICOTLS_DIR = os.path.join(ROOT, "third_party", "picotls")
PICOQUIC_BUILD = os.path.join(PICOQUIC_DIR, "build")
PTLS_INSTALL = os.path.join(PICOQUIC_BUILD, "picotls-install")
PTLS_BUILD = os.path.join(PICOQUIC_BUILD, "picotls-build")


def find_lib(name, search_dirs):
    for d in search_dirs:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


PICOQUIC_LIB_DIRS = [PICOQUIC_BUILD, os.path.join(PICOQUIC_BUILD, "picoquic")]
PTLS_LIB_DIRS = [
    os.path.join(PTLS_INSTALL, "lib"),
    os.path.join(PTLS_INSTALL, "lib64"),
    PTLS_BUILD,
]

picoquic_lib = find_lib("libpicoquic-core.a", PICOQUIC_LIB_DIRS)
if picoquic_lib is None:
    print("ERROR: picoquic not built. Run: ./build_picoquic.sh", file=sys.stderr)
    sys.exit(1)

extra_objects = [picoquic_lib]
for lib_name in ("libpicotls-core.a", "libpicotls-openssl.a", "libpicotls-minicrypto.a"):
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
    os.path.join(PICOQUIC_DIR, "picoquic"),
    os.path.join(PICOTLS_DIR, "include"),
]
library_dirs = []
if openssl_root:
    include_dirs.append(os.path.join(openssl_root, "include"))
    for libdir in ("lib", "lib64"):
        candidate = os.path.join(openssl_root, libdir)
        if os.path.isdir(candidate):
            library_dirs.append(candidate)

# Platform link libs. pthread is implicit on macOS but harmless to list;
# however some macOS toolchains complain about -lpthread, so omit it there.
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
        extra_objects=extra_objects,
        libraries=libraries,
        language="c",
        define_macros=define_macros,
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
