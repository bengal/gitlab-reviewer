"""Persistent library checkouts (app/orchestrator/libraries.py).

The git "remotes" are local bare repos addressed via file:// URLs, so no
network is needed: clone, re-pull (stale marker), fresh-skip, re-pull
failure tolerance, and remote-change re-clone are all exercised for real.
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.orchestrator import libraries


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture()
def lib_remote(tmp_path):
    """A bare repo (the "library remote") with one commit on main, plus the
    work dir used to push new commits."""
    root = tmp_path / "remote"
    root.mkdir()
    bare = root / "liba.git"
    _git(["init", "--bare", "-b", "main", str(bare)], cwd=root)
    work = root / "push"
    work.mkdir()
    _git(["init", "-b", "main", str(work)], cwd=work)
    _git(["config", "user.email", "lib@example.com"], cwd=work)
    _git(["config", "user.name", "Lib"], cwd=work)
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "a.txt"], cwd=work)
    _git(["commit", "-m", "one"], cwd=work)
    first_sha = _git(["rev-parse", "HEAD"], cwd=work).strip()
    _git(["remote", "add", "origin", str(bare)], cwd=work)
    _git(["push", "origin", "main"], cwd=work)
    return {"url": bare.as_uri(), "bare": bare, "work": work, "first_sha": first_sha}


def _push_commit(work: Path, text: str) -> str:
    """Append a commit to the remote's main; returns its sha."""
    (work / "a.txt").write_text(text, encoding="utf-8")
    _git(["add", "a.txt"], cwd=work)
    _git(["commit", "-m", "update"], cwd=work)
    sha = _git(["rev-parse", "HEAD"], cwd=work).strip()
    _git(["push", "origin", "main"], cwd=work)
    return sha


def _backdate(dest: Path) -> None:
    (dest / ".git" / libraries._SYNC_FILE).write_text(f"{time.time() - 10**6}\n", encoding="utf-8")


ENTRY = {"url": None, "ref": "main", "path": "liba"}  # url set per-test


def test_library_relpath_sanitizing():
    assert libraries.library_relpath({"path": "liba"}) == "liba"
    assert libraries.library_relpath({"path": "/group/liba/"}) == "group/liba"
    assert libraries.library_relpath({"path": "a/../b"}) is None
    assert libraries.library_relpath({"path": ""}) is None
    assert libraries.library_relpath({"path": "  "}) is None
    assert libraries.library_relpath({"url": "https://example.com/x"}) is None
    assert libraries.library_relpath("not-a-dict") is None


def test_ensure_clones_missing_checkout(lib_remote, tmp_path):
    entry = dict(ENTRY, url=lib_remote["url"])
    root = tmp_path / "libs"
    logs: list[str] = []
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24, log_line=logs.append)
    assert dest == root / "liba"
    assert (dest / ".git").is_dir()
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (dest / ".git" / libraries._SYNC_FILE).exists()
    assert any("cloning library repo" in line for line in logs)


def test_ensure_fresh_checkout_is_not_pulled(lib_remote, tmp_path):
    entry = dict(ENTRY, url=lib_remote["url"])
    root = tmp_path / "libs"
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)
    _push_commit(lib_remote["work"], "two\n")

    dest2 = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)

    assert dest2 == dest
    head = _git(["rev-parse", "HEAD"], cwd=dest).strip()
    assert head == lib_remote["first_sha"]  # still the old tip
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_ensure_stale_checkout_is_pulled(lib_remote, tmp_path):
    entry = dict(ENTRY, url=lib_remote["url"])
    root = tmp_path / "libs"
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)
    new_sha = _push_commit(lib_remote["work"], "two\n")
    _backdate(dest)

    logs: list[str] = []
    dest2 = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24, log_line=logs.append)

    assert dest2 == dest
    head = _git(["rev-parse", "HEAD"], cwd=dest).strip()
    assert head == new_sha  # re-synced to the new tip
    assert (dest / "a.txt").read_text(encoding="utf-8") == "two\n"
    assert any("re-syncing library checkout" in line for line in logs)


def test_ensure_stale_checkout_without_ref_pulls(lib_remote, tmp_path):
    entry = {"url": lib_remote["url"], "path": "liba"}  # no ref
    root = tmp_path / "libs"
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)
    new_sha = _push_commit(lib_remote["work"], "two\n")
    _backdate(dest)

    libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)

    head = _git(["rev-parse", "HEAD"], cwd=dest).strip()
    assert head == new_sha


def test_ensure_zero_max_age_pulls_every_time(lib_remote, tmp_path):
    entry = dict(ENTRY, url=lib_remote["url"])
    root = tmp_path / "libs"
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=0)
    new_sha = _push_commit(lib_remote["work"], "two\n")

    libraries.ensure_library_checkout(entry, root=root, max_age_hours=0)

    head = _git(["rev-parse", "HEAD"], cwd=dest).strip()
    assert head == new_sha  # no fresh window: pulled again


def test_ensure_refresh_failure_keeps_stale_checkout(lib_remote, tmp_path):
    entry = dict(ENTRY, url=lib_remote["url"])
    root = tmp_path / "libs"
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)
    _push_commit(lib_remote["work"], "two\n")
    _backdate(dest)
    shutil.rmtree(lib_remote["bare"])  # remote gone: the re-pull must fail

    logs: list[str] = []
    dest2 = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24, log_line=logs.append)

    assert dest2 == dest  # stale checkout still usable
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert any("re-sync" in line and "failed" in line for line in logs)


def test_ensure_clone_failure_raises(lib_remote, tmp_path):
    entry = {"url": (tmp_path / "no-such-repo.git").as_uri(), "path": "liba"}
    with pytest.raises(RuntimeError, match="git clone"):
        libraries.ensure_library_checkout(entry, root=tmp_path / "libs", max_age_hours=24)
    assert not (tmp_path / "libs" / "liba").exists() or not (tmp_path / "libs" / "liba" / ".git").is_dir()


def test_ensure_reclones_when_remote_changes(tmp_path):
    """A checkout whose origin points at a different repo is re-cloned."""

    def make_remote(name: str) -> str:
        root = tmp_path / name
        root.mkdir()
        bare = root / "liba.git"
        _git(["init", "--bare", "-b", "main", str(bare)], cwd=root)
        work = root / "push"
        work.mkdir()
        _git(["init", "-b", "main", str(work)], cwd=work)
        _git(["config", "user.email", "lib@example.com"], cwd=work)
        _git(["config", "user.name", "Lib"], cwd=work)
        (work / "a.txt").write_text(name + "\n", encoding="utf-8")
        _git(["add", "a.txt"], cwd=work)
        _git(["commit", "-m", name], cwd=work)
        _git(["remote", "add", "origin", str(bare)], cwd=work)
        _git(["push", "origin", "main"], cwd=work)
        return bare.as_uri()

    first_url = make_remote("first")
    second_url = make_remote("second")

    root = tmp_path / "libs"
    entry = {"url": first_url, "ref": "main", "path": "liba"}
    dest = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)
    assert (dest / "a.txt").read_text(encoding="utf-8") == "first\n"

    entry = dict(entry, url=second_url)
    dest2 = libraries.ensure_library_checkout(entry, root=root, max_age_hours=24)

    assert dest2 == dest
    assert (dest / "a.txt").read_text(encoding="utf-8") == "second\n"


def test_ensure_libraries_returns_mounts(lib_remote, tmp_path):
    mounts = libraries.ensure_libraries(
        [
            {"url": lib_remote["url"], "ref": "main", "path": "liba"},
            # no path: derived "liba" from the URL — the same checkout as the
            # explicit entry above, so it yields no second mount
            {"url": lib_remote["url"]},
            "junk",  # malformed: skipped
        ],
        root=tmp_path / "libs",
        max_age_hours=24,
    )
    assert mounts == [(str(tmp_path / "libs" / "liba"), "liba")]
    assert (tmp_path / "libs" / "liba" / "a.txt").exists()


def test_ensure_libraries_derives_path_from_url(lib_remote, tmp_path):
    """A URL-only entry is checked out at the path derived from the URL
    (…/liba.git -> liba) instead of being skipped."""
    root = tmp_path / "libs"
    logs: list[str] = []
    mounts = libraries.ensure_libraries(
        [{"url": lib_remote["url"], "ref": "main"}], root=root, max_age_hours=24, log_line=logs.append
    )
    assert mounts == [(str(root / "liba"), "liba")]
    assert (root / "liba" / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert any("cloning library repo" in line for line in logs)
    assert not any("skipping" in line for line in logs)
