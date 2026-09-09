"""Integration tests for containers/review-runner/entrypoint.py.

The real stdlib-only entrypoint is executed against a tmp dummy git repo —
the "remote" is a local bare repo addressed via a file:// URL, so no
network is needed — and a fake ``opencode`` executable on PATH. Cases:

a) fake opencode prints a fenced JSON block -> result.json written + parsed,
   review.log written, exit 0;
b) fake opencode prints prose only -> result.md fallback, exit 0;
c) fake opencode sleeps past a tiny REVIEW_TIMEOUT_SECONDS -> exit 124 and a
   partial log;
d) fake opencode exits 1 after dropping a session log file -> the tail of
   that log is appended (scrubbed) to review.log, exit 1;
e) after the run, the entrypoint locates the session opencode "created" in
   the run's working directory (via `opencode session list`) and exports it
   to /out/session.json — with the run's secrets scrubbed out.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "containers/review-runner/entrypoint.py"

SOURCE_BRANCH = "feature-1"
TARGET_BRANCH = "main"
MR_IID = "7"
FAKE_TOKEN = "glpat-faketoken123456789"
PROMPT = "Review the MR diff for correctness and security."


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout


def _make_remote(tmp_path, root_name, *, with_source_branch=True) -> dict:
    """A local stand-in for a GitLab instance: a bare repo at
    <root>/proj.git with two commits — one on main, one on the source branch
    feature-1, plus the MR head ref (as GitLab keeps on the target project's
    remote). With ``with_source_branch=False`` the source branch is absent
    from the remote, mimicking a fork MR."""
    gitlab_root = tmp_path / root_name
    gitlab_root.mkdir()
    bare = gitlab_root / "proj.git"
    _git(["init", "--bare", str(bare)], cwd=gitlab_root)
    _git(["symbolic-ref", "HEAD", f"refs/heads/{TARGET_BRANCH}"], cwd=bare)

    work = gitlab_root / "push"
    work.mkdir()
    _git(["init", "-b", TARGET_BRANCH, str(work)], cwd=work)
    _git(["config", "user.email", "reviewer@example.com"], cwd=work)
    _git(["config", "user.name", "Reviewer"], cwd=work)
    (work / "a.txt").write_text("alpha\n", encoding="utf-8")
    _git(["add", "a.txt"], cwd=work)
    _git(["commit", "-m", "first commit"], cwd=work)
    main_sha = _git(["rev-parse", "HEAD"], cwd=work).strip()
    _git(["checkout", "-b", SOURCE_BRANCH], cwd=work)
    (work / "b.txt").write_text("bravo\n", encoding="utf-8")
    _git(["add", "b.txt"], cwd=work)
    _git(["commit", "-m", "second commit"], cwd=work)
    head_sha = _git(["rev-parse", "HEAD"], cwd=work).strip()

    _git(["remote", "add", "origin", str(bare)], cwd=work)
    refs = [TARGET_BRANCH]
    if with_source_branch:
        refs.append(SOURCE_BRANCH)
    refs.append(f"refs/heads/{SOURCE_BRANCH}:refs/merge-requests/{MR_IID}/head")
    _git(["push", "origin", *refs], cwd=work)
    return {
        "gitlab_url": gitlab_root.as_uri(),  # file:// — no network
        "target_project": "proj",
        "main_sha": main_sha,
        "head_sha": head_sha,
    }


@pytest.fixture()
def gitlab(tmp_path):
    return _make_remote(tmp_path, "gitlab")


@pytest.fixture()
def extra_repo(tmp_path):
    """A small second repo standing in for an extra library project."""
    root = tmp_path / "liba.git"
    _git(["init", "--bare", str(root)], cwd=tmp_path)
    work = tmp_path / "liba-push"
    work.mkdir()
    _git(["init", "-b", "v1.0", str(work)], cwd=work)
    _git(["config", "user.email", "lib@example.com"], cwd=work)
    _git(["config", "user.name", "Lib"], cwd=work)
    (work / "lib.txt").write_text("library\n", encoding="utf-8")
    _git(["add", "lib.txt"], cwd=work)
    _git(["commit", "-m", "lib v1"], cwd=work)
    _git(["remote", "add", "origin", str(root)], cwd=work)
    _git(["push", "origin", "v1.0"], cwd=work)
    return {"url": root.as_uri(), "ref": "v1.0", "path": "liba"}


@pytest.fixture()
def fake_opencode(tmp_path):
    """A fake opencode executable on PATH, switched by FAKE_OPENCODE_MODE.

    It also mimics the session-store commands the entrypoint uses for
    session capture: ``opencode session list --format json`` (a session whose
    directory is the cwd it is called from) and ``opencode export <id>``
    (a fixed export document that deliberately quotes the gitlab token, so
    the scrubbing of /out/session.json is testable).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(
        "#!/bin/sh\n"
        # Record the argv of the run call (the session-capture calls reuse
        # this fake too and must not clobber it).
        'if [ "$1" = run ] && [ -n "${FAKE_OPENCODE_ARGS:-}" ]; then\n'
        '  printf "%s\\n" "$@" > "$FAKE_OPENCODE_ARGS"\n'
        "fi\n"
        'case "$1" in\n'
        "  session)\n"
        '    printf \'[{"id": "ses_fake123", "directory": "%s", "updated": 5}]\\n\' "$PWD"\n'
        "    exit 0\n"
        "    ;;\n"
        "  export)\n"
        '    cat <<EOF\n'
        '{"info": {"id": "$2", "title": "fake review"}, "messages": ['
        '{"role": "user", "parts": [{"type": "text", "text": "Review the MR diff."}]}, '
        '{"role": "assistant", "parts": ['
        '{"type": "reasoning", "text": "thinking with secret ${GITLAB_TOKEN} inside"}, '
        '{"type": "text", "text": "looks fine"}]}]}\n'
        "EOF\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        'case "$FAKE_OPENCODE_MODE" in\n'
        "  json)\n"
        '    echo "analyzing the diff..."\n'
        "    cat <<'EOF'\n"
        "Here is my review of the MR.\n"
        "\n"
        "```json\n"
        '{"summary": "looks good", '
        '"findings": {"critical": [], '
        '"important": [{"file": "b.txt", "line": 1, "description": "nit"}], '
        '"minor": [], '
        '"positive": [{"file": "a.txt", "line": 1, "description": "clear"}]}, '
        '"commit_message_review": "ok", "questions": ["none"]}\n'
        "```\n"
        "EOF\n"
        "    ;;\n"
        "  prose)\n"
        '    echo "Here is my review of the diff."\n'
        '    echo "Overall the change looks reasonable; no blocking issues found."\n'
        "    ;;\n"
        "  stream)\n"
        '    echo "analyzing the diff..."\n'
        '    echo "Thinking: the change is small; verify the new line."\n'
        "    sleep 2\n"
        "    cat <<'EOF'\n"
        "Review done.\n"
        "\n"
        "```json\n"
        '{"summary": "looks good", '
        '"findings": {"critical": [], "important": [], "minor": [], "positive": []}, '
        '"commit_message_review": "ok", "questions": []}\n'
        "```\n"
        "EOF\n"
        "    ;;\n"
        "  nothink)\n"
        '    for arg in "$@"; do\n'
        '      case "$arg" in\n'
        '        --thinking)\n'
        '          echo "unknown option: --thinking"\n'
        '          echo "opencode run [message..]"\n'
        '          echo "Positionals:"\n'
        '          echo "  message  message to send"\n'
        "          exit 1\n"
        "          ;;\n"
        "      esac\n"
        "    done\n"
        '    echo "no --thinking here"\n'
        "    ;;\n"
        "  sleep)\n"
        "    exec sleep 30\n"
        "    ;;\n"
        "  leak)\n"
        '    echo "git: authenticated with password=${GITLAB_TOKEN}"\n'
        '    echo "The model output echoes ${GITLAB_TOKEN} in prose."\n'
        "    ;;\n"
        "  fail)\n"
        '    logdir="$HOME/.local/share/opencode/log"\n'
        '    mkdir -p "$logdir"\n'
        '    echo "session start" > "$logdir/2026-01-01T000000.log"\n'
        '    echo "Error: boom with secret=${GITLAB_TOKEN}" >> "$logdir/2026-01-01T000000.log"\n'
        '    echo "Error: Unexpected error, check log file at $logdir/2026-01-01T000000.log"\n'
        "    exit 1\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _entrypoint_env(
    tmp_path, gitlab, fake_opencode, mode, *, timeout="2", extra_projects=None, extra_env=None
) -> tuple[dict, Path]:
    """The controlled env for one entrypoint run (local file:// git URL)
    plus the out dir it writes to."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{fake_opencode}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GITLAB_URL": gitlab["gitlab_url"],
        "GITLAB_TOKEN": FAKE_TOKEN,
        "TARGET_PROJECT": gitlab["target_project"],
        "MR_IID": MR_IID,
        "MR_SHA": gitlab["head_sha"],
        "SOURCE_BRANCH": SOURCE_BRANCH,
        "TARGET_BRANCH": TARGET_BRANCH,
        "EXTRA_PROJECTS": json.dumps(extra_projects or []),
        "OPENCODE_PROVIDER": "local",
        "OPENCODE_MODEL": "qwen3-32b",
        "OPENCODE_BASE_URL": "http://127.0.0.1:59999/v1",
        "REVIEW_PROMPT": PROMPT,
        "REVIEW_TIMEOUT_SECONDS": timeout,
        "RESULT_PATH": str(out_dir / "result.json"),
        "LOG_PATH": str(out_dir / "review.log"),
        "SESSION_PATH": str(out_dir / "session.json"),
        "WORK_DIR": str(tmp_path / "work"),
        "FAKE_OPENCODE_MODE": mode,
    }
    env.update(extra_env or {})
    return env, out_dir


def _run_entrypoint(
    tmp_path, gitlab, fake_opencode, mode, *, timeout="2", extra_projects=None, extra_env=None
):
    """Run the entrypoint to completion with a controlled env."""
    env, _out_dir = _entrypoint_env(
        tmp_path, gitlab, fake_opencode, mode,
        timeout=timeout, extra_projects=extra_projects, extra_env=extra_env,
    )
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_entrypoint_json_result(tmp_path, gitlab, extra_repo, fake_opencode):
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "json", extra_projects=[extra_repo])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["summary"] == "looks good"
    assert result["findings"]["important"] == [{"file": "b.txt", "line": 1, "description": "nit"}]
    assert result["findings"]["positive"] == [{"file": "a.txt", "line": 1, "description": "clear"}]
    assert result["commit_message_review"] == "ok"
    assert result["questions"] == ["none"]

    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "analyzing the diff..." in log_text  # opencode output captured
    assert log_text.rstrip("\n").endswith("EXIT=0")
    # the token reached git only via the credential-helper env
    assert FAKE_TOKEN not in log_text
    assert FAKE_TOKEN not in proc.stdout

    # target repo cloned @ source branch, base branch fetched for the diff
    work = tmp_path / "work"
    assert (work / "target" / "b.txt").exists()
    source_sha = _git(["rev-parse", "HEAD"], cwd=work / "target").strip()
    base_sha = _git(["rev-parse", f"origin/{TARGET_BRANCH}"], cwd=work / "target").strip()
    assert source_sha == gitlab["head_sha"]
    assert base_sha == gitlab["main_sha"]  # base fetched, distinct from head
    assert base_sha != source_sha

    # extra repo cloned read-only
    lib = work / "lib" / extra_repo["path"]
    assert (lib / "lib.txt").exists()
    assert not os.access(lib / "lib.txt", os.W_OK)

    # opencode.json rendered like the app-side template renderer
    config = json.loads((work / "opencode.json").read_text(encoding="utf-8"))
    assert config["model"] == "local/qwen3-32b"
    assert config["provider"]["local"] == {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "http://127.0.0.1:59999/v1"},
        "models": {"qwen3-32b": {"name": "qwen3-32b"}},
    }
    assert config["permission"] == {
        "bash": "allow",
        "edit": "allow",
        "webfetch": "allow",
        "external_directory": "allow",
    }


def test_entrypoint_model_context_renders_limit(tmp_path, gitlab, fake_opencode):
    """OPENCODE_MODEL_CONTEXT becomes the model's limit (with the fixed
    output headroom) so opencode can auto-compact before the local server's
    context limit is hit; invalid values are ignored."""
    proc = _run_entrypoint(
        tmp_path, gitlab, fake_opencode, "json", extra_env={"OPENCODE_MODEL_CONTEXT": "200000"}
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    config = json.loads((tmp_path / "work" / "opencode.json").read_text(encoding="utf-8"))
    assert config["provider"]["local"]["models"] == {
        "qwen3-32b": {"name": "qwen3-32b", "limit": {"context": 200000, "output": 32768}}
    }

    for bad in ("", "not-a-number", "0", "-5"):
        proc = _run_entrypoint(
            tmp_path, gitlab, fake_opencode, "json", extra_env={"OPENCODE_MODEL_CONTEXT": bad}
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        config = json.loads((tmp_path / "work" / "opencode.json").read_text(encoding="utf-8"))
        assert config["provider"]["local"]["models"] == {"qwen3-32b": {"name": "qwen3-32b"}}


def test_entrypoint_derives_path_for_pathless_extra_project(tmp_path, gitlab, extra_repo, fake_opencode):
    """A URL-only EXTRA_PROJECTS entry (no path) is cloned at the path
    derived from the URL (…/liba.git -> liba), like the app side does."""
    entry = {"url": extra_repo["url"], "ref": extra_repo["ref"]}  # no path
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "json", extra_projects=[entry])
    assert proc.returncode == 0, proc.stdout + proc.stderr

    lib = tmp_path / "work" / "lib" / "liba"
    assert (lib / "lib.txt").exists()
    log_text = (tmp_path / "out" / "review.log").read_text(encoding="utf-8")
    assert "cloning extra library repo" in log_text


def test_entrypoint_uses_pre_mounted_library(tmp_path, gitlab, fake_opencode):
    """When /work/lib/<path> already exists (the app bind-mounts a persistent
    host-side checkout there), the entrypoint must use it as-is and NOT
    clone — the url is deliberately unresolvable, so a clone attempt would
    fail the run."""
    lib = tmp_path / "work" / "lib" / "liba"
    lib.mkdir(parents=True)
    (lib / "mounted.txt").write_text("from host\n", encoding="utf-8")
    proc = _run_entrypoint(
        tmp_path,
        gitlab,
        fake_opencode,
        "json",
        extra_projects=[
            {"url": (tmp_path / "no-such-repo.git").as_uri(), "ref": "main", "path": "liba"}
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert (lib / "mounted.txt").read_text(encoding="utf-8") == "from host\n"
    log_text = (tmp_path / "out" / "review.log").read_text(encoding="utf-8")
    assert "using bind-mounted library checkout" in log_text
    assert "cloning extra library repo" not in log_text


def test_entrypoint_fork_mr_clones_via_mr_ref(tmp_path, fake_opencode):
    """Fork MR: the source branch is not on the target project's remote;
    only refs/merge-requests/<iid>/head reaches the MR head (the clone must
    use the MR ref, not the branch)."""
    remote = _make_remote(tmp_path, "gitlab", with_source_branch=False)
    proc = _run_entrypoint(tmp_path, remote, fake_opencode, "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    work = tmp_path / "work"
    source_sha = _git(["rev-parse", "HEAD"], cwd=work / "target").strip()
    assert source_sha == remote["head_sha"]
    log_text = (tmp_path / "out" / "review.log").read_text(encoding="utf-8")
    assert f"checked out MR head via refs/merge-requests/{MR_IID}/head" in log_text


def test_entrypoint_prose_fallback(tmp_path, gitlab, fake_opencode):
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "prose")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    assert not (out_dir / "result.json").exists()
    markdown = (out_dir / "result.md").read_text(encoding="utf-8")
    assert "Here is my review of the diff." in markdown
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "raw markdown fallback" in log_text
    assert log_text.rstrip("\n").endswith("EXIT=0")


def test_entrypoint_scrubs_secrets_from_host_mounted_files(tmp_path, gitlab, fake_opencode):
    """/out is a host bind mount: opencode output echoing the token must never
    reach review.log, result.md or stdout unscrubbed."""
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "leak")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in log_text
    assert "***" in log_text

    # no fenced json in the leak output -> markdown fallback, also scrubbed
    markdown = (out_dir / "result.md").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in markdown
    assert "***" in markdown

    assert FAKE_TOKEN not in proc.stdout


def test_entrypoint_timeout(tmp_path, gitlab, fake_opencode):
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "sleep", timeout="1")
    assert proc.returncode == 124, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "TIMED OUT" in log_text
    assert log_text.rstrip("\n").endswith("EXIT=124")
    assert not (out_dir / "result.json").exists()
    assert not (out_dir / "result.md").exists()


def test_entrypoint_streams_output_live_and_renders_thinking(tmp_path, gitlab, fake_opencode):
    """opencode's output (including its Thinking: block) is streamed into
    review.log while the run is still in flight, and the final result is
    still extracted from the full output."""
    args_file = tmp_path / "opencode-args.txt"
    env, out_dir = _entrypoint_env(
        tmp_path, gitlab, fake_opencode, "stream",
        timeout="10",
        extra_env={"FAKE_OPENCODE_ARGS": str(args_file)},
    )
    proc = subprocess.Popen(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # the fake opencode sleeps 2s after printing the thinking line: while it
    # is still running, the thinking line must already be in the log
    deadline = time.time() + 10
    seen_thinking_live = False
    while time.time() < deadline:
        time.sleep(0.1)
        log_path = out_dir / "review.log"
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            if "Thinking:" in content and "Review done." not in content:
                seen_thinking_live = True
                break
    assert proc.wait(timeout=90) == 0
    assert seen_thinking_live, "the thinking line was not in review.log while the run was in flight"

    # opencode is invoked with --thinking so reasoning blocks reach the stream
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert "--thinking" in args

    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "analyzing the diff..." in log_text
    assert "Thinking: the change is small; verify the new line." in log_text
    assert "Review done." in log_text
    assert log_text.rstrip("\n").endswith("EXIT=0")

    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["summary"] == "looks good"
    assert FAKE_TOKEN not in log_text
    assert proc.stdout is not None
    assert FAKE_TOKEN not in proc.stdout.read()


def test_entrypoint_no_thinking_flag_fallback(tmp_path, gitlab, fake_opencode):
    """An opencode build without --thinking (unknown option, exit 2) must not
    fail the run: the entrypoint retries without the flag."""
    env, out_dir = _entrypoint_env(tmp_path, gitlab, fake_opencode, "nothink")
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "unknown option: --thinking" in log_text
    assert "retrying without it" in log_text
    assert "no --thinking here" in log_text
    assert log_text.rstrip("\n").endswith("EXIT=0")


def test_entrypoint_long_budget_reaches_opencode(tmp_path, gitlab, fake_opencode):
    """A long REVIEW_TIMEOUT_SECONDS must be passed to opencode in full —
    the first attempt must not be capped (regression: a 60s cap timed out
    legitimate long reviews with EXIT=124)."""
    env, out_dir = _entrypoint_env(
        tmp_path, gitlab, fake_opencode, "nothink", timeout="300",
    )
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "running opencode -m local/qwen3-32b (timeout 300s)" in log_text


def test_entrypoint_opencode_failure_appends_session_log_tail(tmp_path, gitlab, fake_opencode):
    """opencode's 'Unexpected error' points at a log file inside the
    container; its tail must land in review.log (scrubbed) so the failure is
    diagnosable after the container is gone."""
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "fail")
    assert proc.returncode == 1, proc.stdout + proc.stderr

    log_text = (tmp_path / "out" / "review.log").read_text(encoding="utf-8")
    assert "opencode exited with code 1" in log_text
    assert "opencode session log tail" in log_text
    assert "Error: boom with secret=" in log_text
    assert FAKE_TOKEN not in log_text  # session-log lines are scrubbed too
    assert "***" in log_text
    assert log_text.rstrip("\n").endswith("EXIT=1")
    # stdout carries the same scrubbed content
    assert FAKE_TOKEN not in proc.stdout


def test_entrypoint_captures_session_export(tmp_path, gitlab, fake_opencode):
    """The model's session (reasoning blocks + transcript) is exported to
    /out/session.json so it can be inspected/downloaded after the review."""
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    session = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    assert session["info"]["id"] == "ses_fake123"
    assert session["info"]["title"] == "fake review"
    parts = session["messages"][1]["parts"]
    assert [part["type"] for part in parts] == ["reasoning", "text"]
    # the export (untrusted, on the host bind mount) is scrubbed of the token
    assert FAKE_TOKEN not in json.dumps(session)
    assert "***" in json.dumps(session)

    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "wrote opencode session export" in log_text
    assert FAKE_TOKEN not in log_text
    assert FAKE_TOKEN not in proc.stdout


def test_entrypoint_session_capture_failure_does_not_fail_run(tmp_path, gitlab):
    """An opencode without working session commands (or no sessions at all)
    must not fail the run: session.json is simply absent."""
    bin_dir = tmp_path / "plain-bin"
    bin_dir.mkdir()
    (bin_dir / "opencode").write_text(
        "#!/bin/sh\necho 'not json from session list'\nexit 0\n", encoding="utf-8"
    )
    (bin_dir / "opencode").chmod(0o755)

    env, out_dir = _entrypoint_env(tmp_path, gitlab, bin_dir, "prose")
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (out_dir / "session.json").exists()
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "session capture failed" in log_text
