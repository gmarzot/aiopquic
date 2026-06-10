"""Version + build-info reporter for aiopquic.

Usage:
    python -m aiopquic.versions
    aiopquic-versions                    # console-script entry point

Prints aiopquic's installed version plus the picoquic / picotls
submodule revisions captured at build time. Reads `_build_info.py`
written by setup.py during wheel/editable install.

Each line reads `name:  REV (LOC) [DATE]`: parens hold a locator (the
install path for a package, the short SHA for picoquic), brackets the
date. For aiopquic, REV is the installed version and DATE its build
date+time. Submodules (vendored inside aiopquic) are indented under it
as `  - name:`; REV is the most specific identifier available — library
version (picoquic), else upstream branch, else `git describe` / short
SHA — and DATE its pinned-commit date.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from importlib import metadata as _md
from pathlib import Path

from aiopquic import __version__

_HASH_LEN = 8
_LABEL_W = 11


def _abbrev(path: str) -> str:
    """Replace a leading $HOME with ~ for a shorter display path."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _dist(name: str):
    """Return the importlib.metadata Distribution for `name`, preferring
    a site-packages `*.dist-info` over a source-tree `*.egg-info` when
    both are discoverable. importlib.metadata.distribution() returns
    the first match in sys.path order, which loses to stale egg-info
    left behind by older editable installs; explicit enumeration with
    a dist-info bias defends against that shadowing."""
    candidates = [
        d for d in _md.distributions()
        if (d.metadata["Name"] or "").lower() == name.lower()
    ]
    if not candidates:
        return None

    def _key(d):
        loc = getattr(d, "_path", None)
        is_dist_info = loc is not None and str(loc).endswith(".dist-info")
        ver = d.version or ""
        mtime = 0.0
        if loc is not None:
            p = Path(str(loc))
            for fname in ("RECORD", "METADATA"):
                try:
                    mtime = max(mtime, (p / fname).stat().st_mtime)
                except OSError:
                    continue
        return (is_dist_info, ver, mtime)

    candidates.sort(key=_key, reverse=True)
    return candidates[0]


def _is_editable(dist) -> bool:
    """True iff `dist` is a PEP 660 editable install. Reads
    `direct_url.json` written at install time. Falls back to False
    (treat as wheel) when the file or expected fields are absent —
    legacy installs without direct_url.json are treated as wheels."""
    try:
        raw = dist.read_text("direct_url.json")
    except (OSError, AttributeError):
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return bool(data.get("dir_info", {}).get("editable", False))


def _dist_install_date(dist) -> str | None:
    """Install timestamp from a Distribution: mtime of RECORD (written
    last by pip), else METADATA. Returns 'YYYY-MM-DD HH:MM' or None
    when neither is readable."""
    loc = getattr(dist, "_path", None)
    if loc is None:
        return None
    p = Path(str(loc))
    for fname in ("RECORD", "METADATA"):
        try:
            return time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((p / fname).stat().st_mtime),
            )
        except OSError:
            continue
    return None


def _source_newest_mtime(root: str) -> str | None:
    """Newest non-pyc file mtime under `root`. Reflects source-edit
    time for pure-Python editable installs and Cython rebuild time
    for native editable installs."""
    newest = 0.0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                continue
    if not newest:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))


def _build_date(module, name: str | None = None) -> str | None:
    """Best-effort install/build timestamp for a package.

    Wheel installs: dist-info RECORD mtime (the moment pip wrote the
    install) — answers 'when was this version installed' for bug
    reports. Editable installs: newest non-pyc mtime under the package
    dir — answers 'is the running code stale' for active development.
    `name` defaults to the top-level package of `module`."""
    if name is None:
        name = module.__name__.split(".")[0]
    dist = _dist(name)
    if dist is not None and not _is_editable(dist):
        stamped = _dist_install_date(dist)
        if stamped is not None:
            return stamped
    root = os.path.dirname(os.path.abspath(module.__file__))
    return _source_newest_mtime(root)


def _version(v: str) -> str:
    """Installed version with setuptools_scm's `.dYYYYMMDD` dirty-date
    stripped — that day-stamp is redundant with the bracketed build time."""
    return re.sub(r"\.d\d{8}", "", v)


def _meta(module, name: str | None = None) -> str:
    """Suffix after the version: ' (PATH) [BUILD]'. PATH is the
    abbreviated install dir (module.__file__'s parent — the actually-
    imported code, which for editable installs is the source tree and
    for wheels is site-packages/<pkg>). BUILD is the dist-info RECORD
    mtime for wheel installs, newest source mtime for editable installs.
    Either group is dropped when its value is unavailable."""
    out = ""
    src = _abbrev(os.path.dirname(module.__file__))
    if src:
        out += f" ({src})"
    built = _build_date(module, name)
    if built:
        out += f" [{built}]"
    return out


def _compact_describe(describe: str) -> str | None:
    """'v1.1.30-12-g2b1e14d5' -> '1.1.30-12-2b1e14d5'. Returns None when
    describe carries no reachable tag (a bare --always short SHA), so the
    caller falls back to a uniform short SHA."""
    s = describe[1:] if describe.startswith("v") else describe
    m = re.match(r"(.+)-(\d+)-g([0-9a-f]+)$", s)
    if not m:
        return None
    base, dist, sha = m.groups()
    return f"{base}-{dist}-{sha[:_HASH_LEN]}"


def _submodule_info(prefix: str) -> dict[str, str]:
    """Return a {sha, describe, date, subject} dict for the named
    submodule prefix ("PICOQUIC" or "PICOTLS"). Every field defaults
    to "unknown" so older _build_info.py files (sha-only) still work."""
    try:
        from aiopquic import _build_info as _bi
    except ImportError:
        return {"sha": "unknown", "describe": "unknown", "date": "unknown",
                "subject": "unknown", "version": "unknown",
                "branch": "unknown"}
    return {
        "sha":      getattr(_bi, f"{prefix}_SHA",      "unknown"),
        "describe": getattr(_bi, f"{prefix}_DESCRIBE", "unknown"),
        "date":     getattr(_bi, f"{prefix}_DATE",     "unknown"),
        "subject":  getattr(_bi, f"{prefix}_SUBJECT",  "unknown"),
        "version":  getattr(_bi, f"{prefix}_VERSION",  "unknown"),
        "branch":   getattr(_bi, f"{prefix}_BRANCH",   "unknown"),
    }


def _format_submodule(name: str, info: dict[str, str]) -> str:
    label = f"  - {name + ':':<{_LABEL_W}}"
    describe = info["describe"]
    rev = _compact_describe(describe) if describe != "unknown" else None
    if rev is None:
        sha = info["sha"]
        rev = sha[:_HASH_LEN] if sha != "unknown" else "unknown"
    version = info["version"]
    branch = info["branch"]
    if version != "unknown":
        rev = f"{version} ({rev})"
    elif branch != "unknown":
        rev = f"{branch} ({rev})"
    date = info["date"]
    if date != "unknown":
        return f"{label}{rev} [{date[:10]}]"
    return f"{label}{rev}"


def print_versions(file=sys.stdout) -> None:
    import aiopquic
    dist = _dist("aiopquic")
    ver = dist.version if dist is not None else __version__
    print(f"{'aiopquic:':<{_LABEL_W}}{_version(ver)}{_meta(aiopquic, 'aiopquic')}", file=file)
    print(_format_submodule("picoquic", _submodule_info("PICOQUIC")), file=file)
    print(_format_submodule("picotls",  _submodule_info("PICOTLS")),  file=file)


def main() -> int:
    print_versions()
    return 0


if __name__ == "__main__":
    sys.exit(main())
