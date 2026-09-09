"""Persistent host-side checkouts of the extra (library) repos.

Instead of cloning every extra project into each review container, the app
keeps one checkout per library under ``library_checkout_dir`` and bind-mounts
them read-only into the review containers at ``/work/lib/<path>``. Before a
review, each needed checkout is re-synced when it is older than
``library_pull_max_age_hours`` (default 24h; 0 = pull before every review) —
a re-sync is a ``git pull``/``git fetch``, never a re-clone.

The checkout directory must be visible at the SAME path on the host that
creates the review containers: the app passes ``library_checkout_dir``
straight through as the bind-mount source. In the compose/quadlet
deployments it is therefore mounted into the app container at the same path
it has on the host.

Semantics: a failed re-sync of an EXISTING checkout never fails a review
(the stale checkout is reused, with a warning in the run log); a failed
initial clone does (``RuntimeError``) — the review cannot proceed without
the library.

Secrets: the GitLab token reaches git only through the ``GIT_CONFIG_*``
credential-helper environment (never argv), mirroring the review-runner
entrypoint. Locking: one per-checkout thread lock, since concurrent reviews
may need the same library (single app process).
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.config import Settings
from app.services.extra_projects import normalize_extra_projects

log = logging.getLogger(__name__)

# git on a large repo can take a while; the per-review hard timeout does not
# cover this host-side prep step.
_GIT_TIMEOUT_SECONDS = 1800
# Last successful clone/re-sync as an epoch timestamp, kept inside .git so
# it never shows up in the worktree.
_SYNC_FILE = "mrreview-sync"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = _locks[path] = threading.Lock()
        return lock


def library_checkout_root(settings: Settings) -> Path:
    """Root directory for the persistent library checkouts."""
    raw = (settings.library_checkout_dir or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share" / "mr-review" / "libraries"


def library_relpath(entry) -> str | None:
    """The sanitized ``/work/lib``-relative path for one extra-projects
    entry, or None when the entry has no usable ``path`` (the review-runner
    entrypoint skips such entries too, so they are not mounted either)."""
    if not isinstance(entry, dict):
        return None
    path = str(entry.get("path") or "").strip().strip("/")
    if not path or ".." in path.split("/"):
        return None
    return path


def _git_env(gitlab_token: str) -> dict[str, str]:
    """Env for git subprocesses with an argv-safe credential helper (same
    trick as the review-runner entrypoint): the helper body references
    ``$GITLAB_TOKEN`` at run time, so the token itself never appears in any
    process argv — only in this subprocess' environment.
    ``GIT_TERMINAL_PROMPT=0`` makes credential prompts fail fast headless."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if gitlab_token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = (
            "!f() { echo username=gitlab-ci-token; echo password=${GITLAB_TOKEN}; }; f"
        )
        env["GITLAB_TOKEN"] = gitlab_token
    return env


def _run_git(args: list[str], cwd: Path, gitlab_token: str) -> str:
    """Run a git command with the credential-helper env; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(gitlab_token),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stdout.strip()}"
        )
    return proc.stdout


def _sync_time(dest: Path) -> float | None:
    try:
        return float((dest / ".git" / _SYNC_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _mark_synced(dest: Path) -> None:
    try:
        (dest / ".git" / _SYNC_FILE).write_text(f"{time.time():f}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write sync marker in %s: %s", dest, exc)


def _same_remote(dest: Path, url: str, gitlab_token: str) -> bool:
    """True when the checkout's origin matches ``url`` (False for a
    non-checkout or an unreadable remote)."""
    try:
        return _run_git(["remote", "get-url", "origin"], dest, gitlab_token).strip() == url
    except (RuntimeError, OSError):
        return False


def _refresh(dest: Path, url: str, ref: str, gitlab_token: str) -> None:
    """Fast-forward ``dest`` to the tip of ``ref`` (or the default branch)."""
    if ref:
        _run_git(["fetch", "origin", ref], dest, gitlab_token)
        _run_git(["checkout", "--detach", "FETCH_HEAD"], dest, gitlab_token)
    else:
        _run_git(["pull", "--ff-only"], dest, gitlab_token)


def _clone(dest: Path, url: str, ref: str, gitlab_token: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if ref:
        args += ["--branch", ref]
    args += [url, str(dest)]
    _run_git(args, dest.parent, gitlab_token)


def ensure_library_checkout(
    entry,
    *,
    root: Path,
    gitlab_token: str = "",
    max_age_hours: float = 24.0,
    log_line: Callable[[str], None] | None = None,
) -> Path | None:
    """Ensure a checkout for one extra-projects entry; return its path.

    Returns None for entries without a usable url/path. Raises RuntimeError
    (or OSError/subprocess errors) only when an initial clone is needed and
    fails — a failed re-sync of an existing checkout logs a warning and
    reuses the stale checkout.
    """
    relpath = library_relpath(entry)
    if relpath is None:
        if log_line is not None:
            log_line(f"skipping extra project without a usable path: {entry!r}")
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        if log_line is not None:
            log_line(f"skipping extra project without a url: {entry!r}")
        return None
    ref = str(entry.get("ref") or "").strip()
    dest = root / relpath

    with _lock_for(str(dest)):
        if dest.is_dir() and (dest / ".git").is_dir() and _same_remote(dest, url, gitlab_token):
            synced = _sync_time(dest)
            fresh = (
                synced is not None
                and max_age_hours > 0
                and time.time() - synced < max_age_hours * 3600
            )
            if fresh:
                if log_line is not None:
                    log_line(f"library checkout fresh, reusing {dest}")
                return dest
            if log_line is not None:
                log_line(f"re-syncing library checkout {url} (ref {ref or 'default'}) -> {dest}")
            try:
                _refresh(dest, url, ref, gitlab_token)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                log.warning("re-sync of %s failed; using the stale checkout: %s", dest, exc)
                if log_line is not None:
                    log_line(f"warning: re-sync of {url} failed ({exc}); using the existing checkout")
                return dest
            _mark_synced(dest)
            return dest

        # No checkout yet (or one pointing at a different remote): (re)clone.
        if dest.exists():
            shutil.rmtree(dest)
        if log_line is not None:
            log_line(f"cloning library repo {url} (ref {ref or 'default'}) -> {dest}")
        _clone(dest, url, ref, gitlab_token)
        _mark_synced(dest)
        return dest


def ensure_libraries(
    entries,
    *,
    root: Path,
    gitlab_token: str = "",
    max_age_hours: float = 24.0,
    log_line: Callable[[str], None] | None = None,
) -> list[tuple[str, str]]:
    """Ensure a checkout for every extra-projects entry.

    Entries with only a ``url`` get their path derived from it (same
    derivation as the prompt and the container env), so they are checked
    out and mounted instead of being skipped. Returns the
    ``(host_path, /work/lib-relative path)`` pairs to bind-mount read-only
    into the review container (deduplicated: the same library listed twice
    yields one mount).
    """
    mounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in normalize_extra_projects(entries or []):
        dest = ensure_library_checkout(
            entry,
            root=root,
            gitlab_token=gitlab_token,
            max_age_hours=max_age_hours,
            log_line=log_line,
        )
        if dest is not None:
            relpath = library_relpath(entry)
            if relpath is not None and relpath not in seen:
                seen.add(relpath)
                mounts.append((str(dest), relpath))
    return mounts
