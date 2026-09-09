#!/usr/bin/env python3
"""Entrypoint for the review-runner container (Python stdlib only).

Implements the MILESTONES.md "Milestone 5" entrypoint steps exactly:

1. read the run environment (see ``app/orchestrator/run_review.build_env``);
 2. clone the target repo into ``/work/target`` with a git credential helper
    (the token never appears in any argv), check out the MR head via GitLab's
    ``refs/merge-requests/<iid>/head`` ref (which works for fork MRs and
    deleted branches too, falling back to SOURCE_BRANCH) and fetch
    TARGET_BRANCH as ``origin/<target>`` so the MR diff can be produced;
 3. provide each EXTRA_PROJECTS entry read-only at ``/work/lib/<path>``
    (an entry without a path gets one derived from its URL): when the app
    already bind-mounts a persistent host-side checkout there (the current
    app), use it as-is; otherwise clone the repo (standalone / older-app
    fallback);
4. render ``/work/opencode.json`` from ``opencode.json.j2``
   (``string.Template``, stdlib only) so the rendered config is equivalent
   to ``app/orchestrator/opencode_template.render_opencode_json``;
5. build the prompt from REVIEW_PROMPT + diff instructions;
6. run ``opencode run -m <provider>/<model> "<prompt>"`` with
   ``cwd=/work/target``, ``OPENCODE_CONFIG`` pointing at the rendered json,
   ``subprocess.run(timeout=REVIEW_TIMEOUT_SECONDS)``, capturing all output
   to LOG_PATH (default ``/out/review.log``); when opencode exits non-zero
   (not a timeout), the tail of its own session log — the file its
   "Unexpected error" message points at — is appended to LOG_PATH so the
   failure stays diagnosable after the container goes away;
7. extract the LAST fenced ```json block from the output into RESULT_PATH
    (default ``/out/result.json``) after validating it against the
    result_json schema; on parse failure write the raw output to
    ``result.md`` and still exit 0; timeout -> partial log + exit 124;
8. capture opencode's own session (its full transcript, including the
    model's reasoning/"thinking" blocks and tool calls) via
    ``opencode session list`` + ``opencode export <sessionID>`` into
    SESSION_PATH (default ``/out/session.json``);
9. always write a final ``EXIT=<code>`` log line.

Secrets (GITLAB_TOKEN, OPENCODE_API_KEY) never reach any log: /out is a
host bind mount, so everything written there (review.log, result.md,
result.json, session.json) is scrubbed of the run's secret values before
it hits disk — the LLM can echo a secret it saw in the diff, its own
output, or the conversation transcript.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

LOG_PATH = Path(os.environ.get("LOG_PATH", "/out/review.log"))
RESULT_PATH = Path(os.environ.get("RESULT_PATH", "/out/result.json"))
RESULT_MD_PATH = RESULT_PATH.with_name("result.md")
# opencode's session export (transcript + reasoning blocks + tool calls) so
# the model's thinking can be inspected after the review.
SESSION_PATH = Path(os.environ.get("SESSION_PATH", "/out/session.json"))
# opencode's own session logs (where its "Unexpected error" message points).
# Overridable so the entrypoint can be tested; defaults to the location
# opencode uses under $HOME inside the container.
OPENCODE_LOG_DIR = Path(
    os.environ.get("OPENCODE_LOG_DIR") or Path.home() / ".local" / "share" / "opencode" / "log"
)
_CRASH_LOG_TAIL_BYTES = 40_000
# Poll period for the opencode output reader thread (same pattern as
# app/orchestrator/podman_client).
_POLL_SECONDS = 0.05
# WORK_DIR/OPENCODE_TEMPLATE are overridable so the entrypoint can be tested
# outside the container; in production they are /work and /opencode.json.j2.
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work"))
TEMPLATE_PATH = Path(
    os.environ.get("OPENCODE_TEMPLATE") or Path(__file__).resolve().parent / "opencode.json.j2"
)

FINDING_BUCKETS = ("critical", "important", "minor", "positive")

# Same headroom as app/orchestrator/opencode_template.py (kept in sync by
# tests): opencode keeps this many tokens free for the next response, which
# also sizes its auto-compaction trigger.
DEFAULT_MAX_OUTPUT_TOKENS = 32768

_FENCED_JSON_RE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL)

# Secrets shorter than this are not worth scrubbing for (same threshold as
# app/orchestrator/run_review.py); below it, replacement would mangle output.
MIN_SCRUB_LEN = 8

_SCRUB_SECRETS = [
    value
    for value in (os.environ.get("GITLAB_TOKEN"), os.environ.get("OPENCODE_API_KEY"))
    if value and len(value) >= MIN_SCRUB_LEN
]


def scrub(text: str) -> str:
    """Replace each run secret with ``***`` (no-op when none are set)."""
    for secret in _SCRUB_SECRETS:
        text = text.replace(secret, "***")
    return text


def scrub_value(value: object) -> object:
    """Recursively scrub every string in a parsed JSON structure."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(key)): scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


REQUIRED_ENV = (
    "GITLAB_URL",
    "TARGET_PROJECT",
    "MR_IID",
    "SOURCE_BRANCH",
    "TARGET_BRANCH",
    "OPENCODE_PROVIDER",
    "OPENCODE_MODEL",
    "REVIEW_PROMPT",
    "REVIEW_TIMEOUT_SECONDS",
)


def log(message: str) -> None:
    """Append a line to the review log and mirror it on stdout, where the
    podman orchestrator streams it into review_run.log.

    The message is scrubbed of the run's secrets first: LOG_PATH lives on
    the host's bind mount, so the file must not carry raw secret values.
    """
    message = scrub(message)
    print(message, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError as exc:
        print(f"warning: cannot write {LOG_PATH}: {exc}", file=sys.stderr, flush=True)


def _git_env() -> dict[str, str]:
    """Env for git subprocesses with an argv-safe credential helper.

    The helper is injected through the ``GIT_CONFIG_*`` variables (git
    >= 2.31) instead of writing it to argv via ``git config
    credential.helper ...``: the helper body references ``$GITLAB_TOKEN``
    at run time, so the token itself never shows up in any process argv —
    only in this container's environment, which is never logged. Same
    helper for every clone/fetch (per-repo scoping by subprocess env).
    ``GIT_TERMINAL_PROMPT=0`` makes credential prompts fail fast instead
    of hanging headless.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = "!f() { echo username=gitlab-ci-token; echo password=${GITLAB_TOKEN}; }; f"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command with the credential-helper env; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=_git_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stdout.strip()}")
    return proc.stdout


def _target_repo_url() -> str:
    """Clone URL for the target project: ``<GITLAB_URL>/<TARGET_PROJECT>.git``."""
    base = os.environ["GITLAB_URL"].strip().rstrip("/")
    project = os.environ["TARGET_PROJECT"].strip().strip("/")
    return f"{base}/{project}.git"


def _clone_target(target: Path) -> None:
    source = os.environ["SOURCE_BRANCH"]
    target_branch = os.environ["TARGET_BRANCH"]
    mr_iid = os.environ["MR_IID"]
    url = _target_repo_url()
    if target.exists():
        shutil.rmtree(target)
    log(f"cloning {url} -> {target}")
    _run_git(["clone", url, str(target)])
    # Check out the MR head via GitLab's MR ref: it lives on the target
    # project's remote even when the MR comes from a fork (whose source
    # branch is not on this remote) or when the author deleted the branch.
    # Remotes without MR refs fall back to the source branch.
    try:
        _run_git(["fetch", "origin", f"refs/merge-requests/{mr_iid}/head"], cwd=target)
        _run_git(["checkout", "--detach", "FETCH_HEAD"], cwd=target)
        log(f"checked out MR head via refs/merge-requests/{mr_iid}/head")
    except RuntimeError:
        log(f"MR ref refs/merge-requests/{mr_iid}/head unavailable; using branch {source}")
        _run_git(["fetch", "origin", f"{source}:refs/remotes/origin/{source}"], cwd=target)
        _run_git(["checkout", "--detach", f"origin/{source}"], cwd=target)
    _run_git(["fetch", "origin", f"{target_branch}:refs/remotes/origin/{target_branch}"], cwd=target)
    log(f"fetched base branch origin/{target_branch}")


def _make_read_only(root: Path) -> None:
    """Extra repos are read-only context: strip write bits (best effort)."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            try:
                entry.chmod(entry.stat().st_mode & ~0o222)
            except OSError:
                pass
    try:
        root.chmod(root.stat().st_mode & ~0o222)
    except OSError:
        pass


def _derive_path(url: str) -> str:
    """The default /work/lib-relative path for a repo URL: the last
    non-empty segment of the URL's path component, with a trailing ``.git``
    stripped ("" when the URL has no path).

    Mirrors app/services/extra_projects.derive_path — the current app
    applies it before the container starts, so this only matters for
    standalone runs that pass raw URLs.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return ""
    name = segments[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name


def _clone_extra_projects() -> None:
    """Provide each EXTRA_PROJECTS entry read-only at /work/lib/<path>.

    An entry without a path gets one derived from its URL (see
    ``_derive_path``). The current app bind-mounts a persistent host-side
    checkout at that path before the container starts; when the destination
    already exists it is used as-is (it is already read-only). Otherwise the
    repo is cloned (standalone use, or an older app that does not mount
    checkouts).
    """
    raw = os.environ.get("EXTRA_PROJECTS", "").strip()
    if not raw:
        return
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"EXTRA_PROJECTS is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise RuntimeError("EXTRA_PROJECTS must be a JSON list of {url, ref, path} objects")
    for entry in entries:
        if not isinstance(entry, dict):
            log(f"skipping malformed EXTRA_PROJECTS entry: {entry!r}")
            continue
        url = str(entry.get("url") or "").strip()
        ref = str(entry.get("ref") or "").strip()
        path = str(entry.get("path") or "").strip().strip("/")
        if not url:
            log(f"skipping EXTRA_PROJECTS entry without url: {entry!r}")
            continue
        if not path:
            path = _derive_path(url)
            if not path:
                log(f"skipping EXTRA_PROJECTS entry without url/path: {entry!r}")
                continue
        if ".." in path.split("/"):
            log(f"skipping EXTRA_PROJECTS entry with unsafe path: {path!r}")
            continue
        dest = WORK_DIR / "lib" / path
        if dest.exists():
            log(f"using bind-mounted library checkout -> {dest}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning extra library repo {url} (read-only) -> {dest}")
        args = ["clone"]
        if ref:
            args += ["--branch", ref]
        args += [url, str(dest)]
        _run_git(args)
        _make_read_only(dest)


def _render_opencode_config() -> Path:
    """Render opencode.json from the string.Template file.

    Mirrors ``app/orchestrator/opencode_template.render_opencode_json``:
    anthropic -> ``@ai-sdk/anthropic``; anything else (local llama-server)
    -> ``@ai-sdk/openai-compatible`` with ``baseURL``; ``apiKey`` only when
    one is provided; the headless permission block is always allowed.
    Custom (openai-compatible) providers get a ``models`` block declaring the
    profile's model — opencode has no built-in model list for them and fails
    with ProviderModelNotFoundError without it. When OPENCODE_MODEL_CONTEXT is
    set it becomes the model's ``limit.context`` (with a fixed
    ``limit.output``) so opencode can auto-compact before the local server's
    context limit is hit.
    """
    provider = os.environ["OPENCODE_PROVIDER"]
    model_id = os.environ["OPENCODE_MODEL"]
    options: dict[str, str] = {}
    if provider == "anthropic":
        npm = "@ai-sdk/anthropic"
    else:
        npm = "@ai-sdk/openai-compatible"
        options["baseURL"] = os.environ.get("OPENCODE_BASE_URL", "")
    api_key = os.environ.get("OPENCODE_API_KEY", "")
    if api_key:
        options["apiKey"] = api_key

    provider_block: dict[str, object] = {"npm": npm, "options": options}
    if npm == "@ai-sdk/openai-compatible":
        model_entry: dict[str, object] = {"name": model_id}
        context = os.environ.get("OPENCODE_MODEL_CONTEXT", "").strip()
        if context.isdigit() and int(context) > 0:
            # Without a known context window opencode never auto-compacts and
            # a long session dies at the server's hard limit mid-turn.
            model_entry["limit"] = {
                "context": int(context),
                "output": DEFAULT_MAX_OUTPUT_TOKENS,
            }
        provider_block["models"] = {model_id: model_entry}

    rendered = Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(
        provider=provider,
        model=f"{provider}/{model_id}",
        provider_block=json.dumps(provider_block),
    )
    json.loads(rendered)  # fail fast if the template ever breaks the JSON
    config_path = WORK_DIR / "opencode.json"
    config_path.write_text(rendered.rstrip("\n") + "\n", encoding="utf-8")
    log(f"rendered opencode config -> {config_path}")
    return config_path


def _build_prompt() -> str:
    """REVIEW_PROMPT (base prompt + context + output contract) + diff how-to."""
    source = os.environ["SOURCE_BRANCH"]
    target_branch = os.environ["TARGET_BRANCH"]
    diff_instructions = "\n".join(
        (
            "## Working tree and diff",
            f"- The MR head ({source}) is checked out in the current directory.",
            f"- The base branch is available as origin/{target_branch}.",
            f"- Produce the MR diff with: git diff origin/{target_branch}...HEAD",
            "- Any extra library repos are mounted read-only under /work/lib/.",
        )
    )
    return f"{os.environ['REVIEW_PROMPT'].rstrip()}\n\n{diff_instructions}"


def log_opencode_crash_log() -> None:
    """Append the tail of opencode's latest session log to the review log.

    opencode's "Unexpected error" only points at a log file inside the
    container; mirroring its tail here keeps the failure diagnosable from
    the app's stored run log (and from the kept container). Lines go
    through ``log()``, so run secrets are scrubbed. No-op when opencode
    left no log behind.
    """
    try:
        latest = max(OPENCODE_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return
    if latest is None:
        return
    try:
        with latest.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _CRASH_LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return
    lines = tail.splitlines()
    if lines and lines[0] and size > _CRASH_LOG_TAIL_BYTES:
        lines = lines[1:]  # drop the partial first line cut off by the seek
    if lines:
        log(f"opencode session log tail ({latest.name}):")
        for line in lines:
            log(line)


def _is_cli_usage(output: str) -> bool:
    """True when opencode's output looks like CLI usage text (a flag-parsing
    failure, e.g. an unknown ``--thinking`` on an older opencode build)
    rather than a run failure (model/API errors print a JSON payload or
    provider error lines instead)."""
    return "Positionals:" in output


def _run_opencode_once(
    prompt: str, config_path: Path, timeout: float, extra_args: list[str]
) -> tuple[int, str]:
    """Run opencode headless once; return (exit_code, full output). 124 =
    timeout (the child is killed at the deadline; any output streamed before
    it is kept).

    Output is streamed line by line instead of buffered: each line is
    mirrored to stdout (the podman orchestrator streams it into
    ``run.log``) and appended to LOG_PATH as it arrives, so the run's log
    updates in real time and the UI can show the model's progress —
    including the ``Thinking:`` blocks opencode renders for the model's
    reasoning — while the review is still in flight.
    """
    provider = os.environ["OPENCODE_PROVIDER"]
    model_id = os.environ["OPENCODE_MODEL"]
    # -m before the prompt: yargs stops collecting flags at the first
    # positional, so a prompt beginning with "-" would otherwise be parsed
    # as an option.
    command = ["opencode", "run", *extra_args, "-m", f"{provider}/{model_id}", prompt]
    env = dict(os.environ)
    env["OPENCODE_CONFIG"] = str(config_path)
    log(f"running opencode -m {provider}/{model_id} (timeout {timeout:g}s)")
    output: list[str] = []
    proc = subprocess.Popen(
        command,
        cwd=str(WORK_DIR / "target"),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    # A reader thread + queue (like app/orchestrator/podman_client) so the
    # hard deadline is enforced even when opencode is quiet for a while —
    # a plain line-by-line read would only notice the deadline on the next
    # line of output.
    lines: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=_reader, daemon=True, name="opencode-output-reader")
    reader.start()
    deadline = time.monotonic() + timeout
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
        output.append(item)
        log(item.rstrip("\r\n"))
    if timed_out:
        proc.kill()
        # drain what is already buffered so the partial output is complete
        while True:
            try:
                item = lines.get(timeout=0.1)
            except queue.Empty:
                break
            if item is None:
                break
            output.append(item)
            log(item.rstrip("\r\n"))
        reader.join(timeout=2.0)
        proc.wait()
        log(f"TIMED OUT: opencode still running after {timeout:g}s; partial output kept")
        return 124, "".join(output)
    reader.join(timeout=2.0)
    return proc.wait(), "".join(output)


def _run_opencode(prompt: str, config_path: Path, timeout: float) -> tuple[int, str]:
    """Run opencode headless (with ``--thinking`` so the model's reasoning
    blocks are rendered into the output stream) and return
    (exit_code, full output).

    ``--thinking`` requires opencode >= 1.18.30 (see the pinned version in
    the Containerfile). When an older build rejects the flag — it prints CLI
    usage text and exits non-zero — the run is retried once without it, so
    the review still happens (just without the thinking blocks) instead of
    failing on the flag. The retry gets the remaining timeout budget.
    """
    start = time.monotonic()
    # The first attempt gets the full budget: a --thinking rejection is
    # instant, so capping it would only truncate legitimate long reviews
    # (and the retry below still gets the remaining time).
    exit_code, output = _run_opencode_once(prompt, config_path, timeout, ["--thinking"])
    if exit_code not in (0, 124) and _is_cli_usage(output):
        log(f"opencode rejected --thinking (exit {exit_code}); retrying without it")
        remaining = max(timeout - (time.monotonic() - start), 0.0)
        exit_code, output = _run_opencode_once(prompt, config_path, remaining, [])
    return exit_code, output


def _list_opencode_sessions(cwd: Path) -> list | None:
    """``opencode session list --format json`` from ``cwd`` (or None on
    failure). opencode scopes the listing to its project root, which is the
    nearest directory *at or above* cwd that holds a session database — so
    the caller walks up from the work dir until the run's session shows up.
    """
    try:
        listing = subprocess.run(
            ["opencode", "session", "list", "--format", "json"],
            cwd=str(cwd),
            env=dict(os.environ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"session capture failed (list): {exc}")
        return None
    if listing.returncode != 0:
        log(f"session capture failed: `opencode session list` exit {listing.returncode}")
        return None
    try:
        sessions = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError as exc:
        log(f"session capture failed: unparsable session list: {exc}")
        return None
    return sessions if isinstance(sessions, list) else []


def _newest_session_id(sessions: list) -> str:
    """The most recently updated session id of a parsed listing ("" when
    there is none)."""
    def sort_key(entry: object) -> float:
        if not isinstance(entry, dict):
            return 0.0
        updated = entry.get("updated") or entry.get("created") or 0
        try:
            return float(updated)
        except (TypeError, ValueError):
            return 0.0

    newest = max(sessions, key=sort_key, default=None)
    return str(newest.get("id") or "").strip() if isinstance(newest, dict) else ""


def _capture_session() -> None:
    """Export opencode's session for this run into SESSION_PATH.

    opencode's own session store (its transcript, including the model's
    reasoning/"thinking" blocks and every tool call) lives under
    ``$HOME/.local/share/opencode`` inside the container and would otherwise
    vanish with the ephemeral run. We locate the session this run just
    created and ``opencode export`` it to SESSION_PATH — which sits on the
    host bind mount, so it is scrubbed of the run's secrets before it hits
    disk (the transcript can quote material that carried a secret).

    The session's recorded directory is opencode's project root — the
    nearest directory at or above the work dir with a session database —
    not always the work dir itself, so the lookup walks up from
    WORK_DIR/target to WORK_DIR (then to the filesystem root as a last
    resort). Best effort: any failure (no sessions, opencode not exporting,
    a non-JSON export) is logged and swallowed so it can never fail the run
    — the review result is what matters, the session is diagnostic.
    """
    if shutil.which("opencode") is None:
        log("session capture skipped: opencode not on PATH")
        return
    target_dir = WORK_DIR / "target"
    session_id = ""
    found_in: Path = target_dir
    try:
        # Walk up from the work dir (bounded by its depth): opencode scopes
        # the listing to the project root — the nearest directory at or
        # above cwd that holds a session database.
        seen: set[Path] = set()
        cwd: Path | None = target_dir
        while cwd is not None and cwd not in seen and not session_id:
            seen.add(cwd)
            sessions = _list_opencode_sessions(cwd)
            if sessions is None:
                return  # a hard failure above already logged
            if sessions:
                session_id = _newest_session_id(sessions)
                found_in = cwd
            parent = cwd.parent
            cwd = parent if parent != cwd else None
        if not session_id:
            # last resort: the most recent session in the global listing
            home = Path.home()
            global_sessions = _list_opencode_sessions(home)
            if global_sessions:
                session_id = _newest_session_id(global_sessions)
                found_in = home
    except Exception as exc:  # capture must never fail the run
        log(f"session capture failed: {exc!r}")
        return
    if not session_id:
        log("session capture: no opencode session found")
        return
    # No --sanitize: it would redact transcript/file data, which is exactly
    # what the inspection is for; run secrets are scrubbed below like every
    # other file written to the /out bind mount.
    try:
        export = subprocess.run(
            ["opencode", "export", session_id],
            cwd=str(found_in),
            env=dict(os.environ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"session capture failed (export): {exc}")
        return
    if export.returncode != 0:
        log(f"session capture failed: `opencode export {session_id}` exit {export.returncode}")
        return
    try:
        data = json.loads(export.stdout or "")
    except json.JSONDecodeError as exc:
        log(f"session capture failed: unparsable export: {exc}")
        return
    if not isinstance(data, dict):
        log("session capture: export is not a JSON object")
        return
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        # /out is a host bind mount: scrub the (untrusted) transcript before
        # it hits the host filesystem — it may quote secrets from the diff or
        # from the conversation itself.
        rendered = json.dumps(scrub_value(data), indent=2, ensure_ascii=False) + "\n"
        SESSION_PATH.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        log(f"session capture: cannot write {SESSION_PATH}: {exc}")
        return
    log(f"wrote opencode session export -> {SESSION_PATH}")


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_result(data: object) -> dict[str, object]:
    """Validate the fenced JSON against the result_json schema and normalize
    it. Tolerant, like app/schemas/result: missing keys get defaults, wrong
    types are coerced. Raises ValueError when it cannot be a result."""
    if not isinstance(data, dict):
        raise ValueError("result block must be a JSON object")
    findings = data.get("findings")
    if findings is None:
        findings = {}
    elif isinstance(findings, list):
        findings = {"important": findings}
    if not isinstance(findings, dict):
        raise ValueError("'findings' must be a JSON object")
    normalized_findings: dict[str, list[dict[str, object]]] = {}
    for bucket in FINDING_BUCKETS:
        items = findings.get(bucket) or []
        if not isinstance(items, list):
            raise ValueError(f"'findings.{bucket}' must be a list")
        normalized_findings[bucket] = [
            item if isinstance(item, dict) else {"description": _coerce_text(item)} for item in items
        ]
    questions = data.get("questions")
    if questions is None:
        questions = []
    elif isinstance(questions, str):
        questions = [questions] if questions.strip() else []
    elif not isinstance(questions, list):
        questions = [questions]
    return {
        "summary": _coerce_text(data.get("summary")),
        "findings": normalized_findings,
        "commit_message_review": _coerce_text(data.get("commit_message_review")),
        "questions": [_coerce_text(question) for question in questions if question is not None],
    }


def _extract_result(text: str) -> dict[str, object] | None:
    """The LAST fenced ```json block of ``text``, schema-validated; None when
    absent or unparseable."""
    matches = _FENCED_JSON_RE.findall(text)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    try:
        return _normalize_result(data)
    except ValueError:
        return None


def run() -> int:
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        log(f"ERROR: missing required environment variables: {', '.join(missing)}")
        return 1
    try:
        timeout = float(os.environ["REVIEW_TIMEOUT_SECONDS"])
    except ValueError:
        log(f"ERROR: REVIEW_TIMEOUT_SECONDS is not a number: {os.environ['REVIEW_TIMEOUT_SECONDS']!r}")
        return 1

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        _clone_target(WORK_DIR / "target")
        _clone_extra_projects()
        config_path = _render_opencode_config()
        prompt = _build_prompt()
    except (OSError, RuntimeError) as exc:
        log(f"ERROR: {exc}")
        return 1

    exit_code, output = _run_opencode(prompt, config_path, timeout)
    if output:
        log(output.rstrip("\n"))
    if exit_code not in (0, 124):
        log(f"opencode exited with code {exit_code}")
        log_opencode_crash_log()

    result = _extract_result(output)
    if result is not None:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        # /out is a host bind mount: scrub the (untrusted) LLM result before
        # it hits the host filesystem.
        rendered = json.dumps(scrub_value(result), indent=2, ensure_ascii=False) + "\n"
        RESULT_PATH.write_text(rendered, encoding="utf-8")
        log(f"wrote structured result -> {RESULT_PATH}")
    elif output.strip():
        RESULT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_MD_PATH.write_text(scrub(output), encoding="utf-8")
        log(f"no valid fenced json block; wrote raw markdown fallback -> {RESULT_MD_PATH}")
    else:
        log("no valid fenced json block and no output to fall back to")

    _capture_session()
    return exit_code


def main() -> int:
    try:
        code = run()
    except Exception as exc:  # the entrypoint must always end with an EXIT line
        log(f"ERROR: unexpected failure: {exc!r}")
        code = 1
    log(f"EXIT={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
