"""Podman orchestrator: one short-lived ``podman run`` container per review.

Container lifecycle: containers are NOT removed with ``--rm``. Successful
runs are removed right after their results are read; failed/timed-out runs
are kept so the container's own state — in particular opencode's log under
``/home/reviewer/.local/share/opencode/log`` — can be inspected (``podman
ps -a --filter name=mr-review-`` + ``podman cp``). Stale exited review
containers older than 24h are purged at the start of each run.

Security model (see PLAN "Key risks"):
- the image must match the allowlist (``REVIEW_IMAGE_ALLOWLIST``, which
  defaults to ``REVIEW_IMAGE`` itself) or the run is refused;
- resource caps (``--memory``/``--cpus``/``--pids-limit``) plus
  ``--security-opt no-new-privileges``; ``--privileged`` is never used;
- ``--userns=keep-id`` maps the launching host user to the same uid inside
  the container, so the app-created ``/out`` bind mount (mode 0700, owned by
  the app user) is writable by the container's non-root ``reviewer`` user —
  under the default rootless userns the host uid lands on container uid 0
  and every write to /out fails with EACCES;
- the ``/out`` bind mount carries the ``Z`` (private) SELinux option so
  enforcing hosts relabel it ``container_file_t``; without that the
  ``container_t`` domain is denied the host's ``user_tmp_t`` label and the
  same EACCES occurs even with correct ownership;
- the persistent library checkouts (``LIBRARY_CHECKOUT_DIR``, see
  ``app.orchestrator.libraries``) are bound into ``/work/lib/<path>`` with
  ``ro`` — the model can read cross-repo context but never modify the
  shared host checkouts;
- secrets travel only in the subprocess environment (argv carries ``-e KEY``
  names, never values);
- streamed stdout/stderr is scrubbed of secret values before reaching the
  ``log_chunk`` callback;
- a hard timeout kills the run and marks the outcome ``timed_out``.
"""

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.db import get_db_session, get_settings_row
from app.models import MergeRequest, ModelProfile
from app.orchestrator import RunOutcome
from app.orchestrator.libraries import ensure_libraries, library_checkout_root
from app.orchestrator.run_review import build_env, scrub_secrets, secret_env_values
from app.security import decrypt_secret

_POLL_SECONDS = 0.05
_STALE_CONTAINER_SECONDS = 24 * 3600
# ``podman ps --format '{{.ID}} {{.CreatedAt}}'`` -> e.g.
# "f05a316845dc 2026-09-05 09:09:50.355623967 +0200 CEST"
_CONTAINER_CREATED_RE = re.compile(
    r"^([0-9a-f]+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.\d+)?\s+([+-]\d{4})"
)


def image_allowed(image: str, allowlist: list[str]) -> bool:
    """True when ``image`` equals an entry or sits under an entry's
    repo/tag prefix (entry may be a full name, a name without tag, or a repo)."""
    return any(
        image == entry or image.startswith(entry + "/") or image.startswith(entry + ":")
        for entry in allowlist
    )


class PodmanOrchestrator:
    """Executes reviews as ephemeral podman containers (host user socket)."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        image: str | None = None,
        network: str | None = None,
        timeout_seconds: int | float | None = None,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        podman_bin: str = "podman",
    ):
        self._settings = settings or get_settings()
        self._image = image or self._settings.review_image
        self._network = network or self._settings.podman_network
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else self._settings.review_timeout_seconds
        )
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._podman_bin = podman_bin
        if not image_allowed(self._image, self._allowlist()):
            raise ValueError(f"review image {self._image!r} is not in the allowlist {self._allowlist()}")

    def _allowlist(self) -> list[str]:
        raw = self._settings.review_image_allowlist or ""
        entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
        return entries or [self._settings.review_image]

    def build_argv(
        self,
        *,
        env: dict[str, str],
        out_dir: str | Path,
        name: str,
        lib_mounts: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        """``podman run`` argv for one run (no ``--rm``: see module docstring
        for the keep-on-failure lifecycle).

        ``lib_mounts`` is a list of ``(host_path, /work/lib-relative path)``
        pairs for the persistent library checkouts, bound read-only (they are
        context the model may read but never write).

        Env values are deliberately NOT in argv: each key is passed as
        ``-e KEY`` (podman picks the value up from this process' environment,
        which the caller sets via the subprocess ``env=`` argument).
        """
        argv = [self._podman_bin, "run"]
        argv.append(f"--memory={self._memory}")
        argv.append(f"--cpus={self._cpus}")
        argv.append(f"--pids-limit={self._pids_limit}")
        argv += ["--security-opt", "no-new-privileges"]
        # Keep the launching user's uid inside the container so the 0700
        # mkdtemp'd out dir stays writable by the image's reviewer user
        # (both uid 1000 by deployment design).
        argv.append("--userns=keep-id")
        argv.append(f"--network={self._network}")
        argv.append(f"--name={name}")
        for key in env:
            argv += ["-e", key]
        # Z: private SELinux relabel (container_file_t) for enforcing hosts;
        # a no-op where SELinux is off.
        argv += ["--mount", f"type=bind,src={out_dir},dst=/out,Z"]
        for host_path, lib_relpath in lib_mounts or []:
            argv += ["--mount", f"type=bind,src={host_path},dst=/work/lib/{lib_relpath},ro,Z"]
        argv.append(self._image)
        return argv

    def run_review(
        self,
        run,
        job,
        *,
        log_chunk: Callable[[str], None] | None = None,
    ) -> RunOutcome:
        with get_db_session() as db:
            mr = db.get(MergeRequest, job.merge_request_id)
            profile = db.get(ModelProfile, job.model_profile_id)
            settings_row = get_settings_row(db)
        if mr is None or profile is None:
            return RunOutcome(exit_code=1, error="MR or model profile missing for this job")

        self._purge_stale_containers()
        env = build_env(job, profile, settings_row, mr=mr)
        scrub_with = secret_env_values(env)

        def emit(line: str) -> None:
            if log_chunk is not None:
                log_chunk(scrub_secrets(line, scrub_with))

        # Persistent host-side checkouts of the extra (library) repos,
        # mounted read-only into the container (see app.orchestrator.libraries).
        lib_mounts: list[tuple[str, str]]
        try:
            lib_mounts = ensure_libraries(
                job.extra_projects or [],
                root=library_checkout_root(self._settings),
                gitlab_token=decrypt_secret(settings_row.gitlab_token)
                if settings_row.gitlab_token
                else "",
                max_age_hours=self._settings.library_pull_max_age_hours,
                log_line=emit,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return RunOutcome(exit_code=1, error=f"library checkout failed: {exc}")

        out_dir = Path(tempfile.mkdtemp(prefix="mr-review-out-"))
        name = f"mr-review-{run.id or 0}-{secrets.token_hex(4)}"
        argv = self.build_argv(env=env, out_dir=out_dir, name=name, lib_mounts=lib_mounts)

        try:
            timed_out, exit_code = self._run_streaming(argv, env, emit)
            result_json = self._read_json(out_dir / "result.json")
            result_markdown = self._read_text(out_dir / "result.md")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return RunOutcome(exit_code=125, error=f"podman run failed: {exc}")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

        if timed_out:
            error = f"review timed out after {self._timeout:g}s"
        elif exit_code != 0:
            error = f"review container exited with code {exit_code}"
        else:
            error = None
            self._remove_container(name)  # nothing left to inspect on success
        return RunOutcome(
            exit_code=exit_code,
            result_json=result_json,
            result_markdown=result_markdown,
            error=error,
            timed_out=timed_out,
        )

    def _remove_container(self, name: str) -> None:
        """Best-effort ``podman rm`` for an exited review container."""
        try:
            subprocess.run(
                [self._podman_bin, "rm", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _purge_stale_containers(self) -> None:
        """Remove exited/dead review containers older than 24h (best effort).

        Failed and timed-out runs keep their container for post-mortems, so
        without this they would accumulate one NM-clone-sized layer per run.
        """
        try:
            proc = subprocess.run(
                [
                    self._podman_bin,
                    "ps",
                    "-a",
                    "--filter",
                    "name=mr-review-",
                    "--filter",
                    "status=exited",
                    "--filter",
                    "status=dead",
                    "--format",
                    "{{.ID}} {{.CreatedAt}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if proc.returncode != 0:
            return
        cutoff = time.time() - _STALE_CONTAINER_SECONDS
        for line in proc.stdout.splitlines():
            match = _CONTAINER_CREATED_RE.match(line)
            if match is None:
                continue
            created = datetime.fromisoformat(
                f"{match.group(2)} {match.group(3)}{match.group(4)[:3]}:{match.group(4)[3:]}"
            ).timestamp()
            if created < cutoff:
                self._remove_container(match.group(1))

    def _run_streaming(
        self,
        argv: list[str],
        env: dict[str, str],
        emit,
    ) -> tuple[bool, int]:
        """Run podman, streaming stdout+stderr lines to ``emit``.

        Returns (timed_out, exit_code). On timeout the container is killed and
        any remaining buffered lines are still emitted (partial log).
        """
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, **env},
            text=True,
        )
        lines: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=_reader, daemon=True, name="podman-log-reader")
        reader.start()

        deadline = time.monotonic() + self._timeout
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = lines.get(timeout=min(_POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            if item is None:
                break
            emit(item.rstrip("\r\n"))

        if timed_out:
            proc.kill()
            while True:
                try:
                    item = lines.get(timeout=0.1)
                except queue.Empty:
                    break
                if item is None:
                    break
                emit(item.rstrip("\r\n"))
        reader.join(timeout=2.0)
        return timed_out, proc.wait()

    @staticmethod
    def _read_json(path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_text(path: Path):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
