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
extensions = [
    Extension(
        "aiopquic._binding._transport",
        sources=[os.path.join("src", "aiopquic", "_binding", "_transport.pyx")],
        include_dirs=[
            os.path.join(ROOT, "src", "aiopquic", "_binding"),
            PICOQUIC_INC,
            PICOTLS_INC,
        ],
        extra_objects=extra_objects,
        libraries=["ssl", "crypto", "pthread"],
        language="c",
        define_macros=[("_GNU_SOURCE", "1")],
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
