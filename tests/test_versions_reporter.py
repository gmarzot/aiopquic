"""Version reporter: git state and extension freshness.

A from-source install imports from the tree, but its dist metadata
version is written once at install time, so it cannot track a branch
switch or an uncommitted edit. The reporter consults git to answer "what
am I running" — and must not do so for a published wheel, where the
metadata version is already accurate and an enclosing repo would be
misattributed.

aiopquic carries a second question aiomoqt does not: the binding is
compiled, so a checked-out source is not necessarily the code running.
`_ext_stale` is what catches a header edited without a rebuild, which
otherwise invalidates any measurement taken against the old `.so`.

Asserted against synthetic trees, so nothing depends on the branch or
cleanliness of the checkout under test.
"""
import io
import os
import shutil
import subprocess

import pytest

from aiopquic import versions as V

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not available")


def _repo(tmp_path, name, *, src_layout=True):
    """A minimal package tree that is its own git repo. Mirrors
    aiopquic's own src layout by default. Returns the package dir."""
    root = tmp_path / name
    mod = name.replace("-", "_")
    pkg = (root / "src" / mod) if src_layout else (root / mod)
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.1"\n')
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "add", "-A"), cwd=root, check=True)
    subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"), cwd=root, check=True)
    return pkg


def _head(pkg):
    return subprocess.run(("git", "-C", str(pkg), "rev-parse", "HEAD"),
                          capture_output=True, text=True).stdout.strip()


def _write(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


class TestOwnSourceTree:
    """The guard that keeps an unrelated enclosing repo out of the line."""

    def test_matches_src_layout(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg", src_layout=True)
        assert V._own_source_tree(str(pkg), "mypkg") is True

    def test_matches_flat_layout(self, tmp_path):
        pkg = _repo(tmp_path, "flatpkg", src_layout=False)
        assert V._own_source_tree(str(pkg), "flatpkg") is True

    def test_rejects_a_different_package(self, tmp_path):
        """A wheel installed into a venv inside someone else's project
        must not report that project's revision."""
        pkg = _repo(tmp_path, "someone-elses-app")
        assert V._own_source_tree(str(pkg), "aiopquic") is False

    def test_rejects_a_non_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert V._own_source_tree(str(plain), "aiopquic") is False

    def test_rejects_a_repo_without_pyproject(self, tmp_path):
        pkg = _repo(tmp_path, "nopyproject")
        (pkg.parent.parent / "pyproject.toml").unlink()
        assert V._own_source_tree(str(pkg), "nopyproject") is False


class TestExtStale:
    """Compiled-vs-source freshness. A stale .so silently invalidates
    any benchmark run against it."""

    def test_fresh_when_so_is_newer(self, tmp_path):
        _write(tmp_path / "c" / "callback.h", 1000)
        _write(tmp_path / "_transport.so", 2000)
        assert V._ext_stale(str(tmp_path)) is False

    def test_stale_when_header_is_newer(self, tmp_path):
        _write(tmp_path / "_transport.so", 1000)
        _write(tmp_path / "c" / "callback.h", 2000)
        assert V._ext_stale(str(tmp_path)) is True

    @pytest.mark.parametrize("ext", [".pyx", ".pxd", ".h"])
    def test_every_hand_written_source_counts(self, tmp_path, ext):
        _write(tmp_path / "_transport.so", 1000)
        _write(tmp_path / f"src{ext}", 2000)
        assert V._ext_stale(str(tmp_path)) is True

    def test_generated_c_is_excluded(self, tmp_path):
        """`.c` is regenerated on build, so its mtime tracks the .so
        rather than an author's edit — counting it would report stale on
        every successful build."""
        _write(tmp_path / "_transport.so", 1000)
        _write(tmp_path / "_transport.c", 2000)
        assert V._ext_stale(str(tmp_path)) is False

    def test_no_extension_is_not_stale(self, tmp_path):
        """Nothing built means nothing to be stale against."""
        _write(tmp_path / "c" / "callback.h", 2000)
        assert V._ext_stale(str(tmp_path)) is False

    def test_no_native_source_is_not_stale(self, tmp_path):
        _write(tmp_path / "_transport.so", 1000)
        assert V._ext_stale(str(tmp_path)) is False

    def test_pycache_is_ignored(self, tmp_path):
        _write(tmp_path / "_transport.so", 1000)
        _write(tmp_path / "__pycache__" / "stale.h", 5000)
        assert V._ext_stale(str(tmp_path)) is False


class TestGitState:

    def test_reports_revision_and_branch(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), "0.0.1", "mypkg")
        assert out is not None and out.startswith(" git:")
        assert _head(pkg).startswith(
            out.split("git:")[1].split()[0].removesuffix("-dirty"))

    def test_none_outside_a_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert V._git_state(str(plain), "0.0.1", "mypkg") is None

    def test_none_for_an_unrelated_repo(self, tmp_path):
        pkg = _repo(tmp_path, "someone-elses-app")
        assert V._git_state(str(pkg), "0.0.1", "aiopquic") is None

    def test_dirty_marks_uncommitted_edits(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        assert "-dirty" not in V._git_state(str(pkg), "0.0.1", "mypkg")
        (pkg / "__init__.py").write_text("# edited\n")
        assert "-dirty" in V._git_state(str(pkg), "0.0.1", "mypkg")

    def test_stale_when_version_names_another_commit(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), "0.1.0.dev3+gdeadbee", "mypkg")
        assert "metadata version STALE" in out

    def test_not_stale_when_version_names_head(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        out = V._git_state(str(pkg), f"0.1.0.dev3+g{_head(pkg)[:9]}", "mypkg")
        assert "STALE" not in out

    def test_no_version_hash_is_not_stale(self, tmp_path):
        """A plain release version names no commit, so it cannot disagree."""
        pkg = _repo(tmp_path, "mypkg")
        assert "STALE" not in V._git_state(str(pkg), "0.12.0", "mypkg")

    def test_extension_staleness_is_reported(self, tmp_path):
        pkg = _repo(tmp_path, "mypkg")
        _write(pkg / "_transport.so", 1000)
        _write(pkg / "c" / "callback.h", 2000)
        assert "EXTENSION STALE" in V._git_state(str(pkg), "0.0.1", "mypkg")


class TestReporterDegradation:
    """print_versions must never fail, whatever the install shape."""

    def test_without_git_falls_back_to_metadata(self, monkeypatch):
        monkeypatch.setattr(V, "_git", lambda *a, **k: None)
        buf = io.StringIO()
        V.print_versions(file=buf)
        out = buf.getvalue()
        assert "aiopquic:" in out
        assert "git:" not in out

    def test_wheel_install_consults_no_git(self, monkeypatch):
        """A published wheel already carries an accurate build-time
        version; consulting git could only misattribute one."""
        monkeypatch.setattr(V, "_is_editable", lambda dist: False)
        called = []
        monkeypatch.setattr(V, "_git",
                            lambda *a, **k: called.append(a) or None)
        buf = io.StringIO()
        V.print_versions(file=buf)
        assert "git:" not in buf.getvalue()
        assert called == []

    def test_submodule_lines_still_printed(self, monkeypatch):
        """The git additions must not displace picoquic/picotls."""
        monkeypatch.setattr(V, "_git", lambda *a, **k: None)
        buf = io.StringIO()
        V.print_versions(file=buf)
        out = buf.getvalue()
        assert "picoquic:" in out and "picotls:" in out
