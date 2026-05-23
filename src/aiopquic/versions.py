"""Version + build-info reporter for aiopquic.

Usage:
    python -m aiopquic.versions
    aiopquic-versions                    # console-script entry point

Prints aiopquic's installed version + the picoquic / picotls submodule
revisions captured at build time (git describe + commit date + subject,
or raw SHA fallback for older builds). Reads `_build_info.py` written
by setup.py during wheel/editable install.
"""
from __future__ import annotations

import os
import sys

from aiopquic import __version__


def _submodule_info(prefix: str) -> dict[str, str]:
    """Return a {sha, describe, date, subject} dict for the named
    submodule prefix ("PICOQUIC" or "PICOTLS"). Every field defaults
    to "unknown" so older _build_info.py files (sha-only) still work."""
    try:
        from aiopquic import _build_info as _bi
    except ImportError:
        return {"sha": "unknown", "describe": "unknown",
                "date": "unknown", "subject": "unknown"}
    return {
        "sha":      getattr(_bi, f"{prefix}_SHA",      "unknown"),
        "describe": getattr(_bi, f"{prefix}_DESCRIBE", "unknown"),
        "date":     getattr(_bi, f"{prefix}_DATE",     "unknown"),
        "subject":  getattr(_bi, f"{prefix}_SUBJECT",  "unknown"),
    }


def _format_submodule(name: str, info: dict[str, str]) -> str:
    # Prefer the human-readable describe + date; fall back to the raw
    # SHA when older _build_info.py only captured that.
    if info["describe"] != "unknown":
        line = f"{name:<8} {info['describe']}"
        if info["date"] != "unknown":
            line += f"  ({info['date']})"
        if info["subject"] != "unknown":
            line += f"\n         {info['subject']}"
        return line
    return f"{name:<8} {info['sha']}"


def print_versions(file=sys.stdout) -> None:
    import aiopquic
    src = os.path.dirname(aiopquic.__file__)
    print(f"aiopquic {__version__}", file=file)
    print(f"         {src}", file=file)
    print(_format_submodule("picoquic", _submodule_info("PICOQUIC")), file=file)
    print(_format_submodule("picotls",  _submodule_info("PICOTLS")),  file=file)


def main() -> int:
    print_versions()
    return 0


if __name__ == "__main__":
    sys.exit(main())
