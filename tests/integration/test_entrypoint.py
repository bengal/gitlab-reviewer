"""Integration tests for containers/review-runner/entrypoint.py.

The real stdlib-only entrypoint is executed against a tmp dummy git repo —
the "remote" is a local bare repo addressed via a file:// URL, so no
network is needed — and a fake ``opencode`` executable on PATH. Cases:

a) fake opencode prints a fenced JSON block -> result.json written + parsed,
   review.log written, exit 0;
b) fake opencode prints prose only -> result.md fallback, exit 0;
c) fake opencode sleeps past a tiny REVIEW_TIMEOUT_SECONDS -> exit 124 and a
   partial log.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "containers/review-runner/entrypoint.py"

SOURCE_BRANCH = "feature-1"
TARGET_BRANCH = "main"
FAKE_TOKEN = "glpat-faketoken123456789"
PROMPT = "Review the MR diff for correctness and security."


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture()
def gitlab(tmp_path):
    """A local stand-in for a GitLab instance: a bare repo at <root>/proj.git
    with two commits — one on main, one on the source branch feature-1."""
    gitlab_root = tmp_path / "gitlab"
    gitlab_root.mkdir()
    _git(["init", "--bare", str(gitlab_root / "proj.git")], cwd=gitlab_root)

    work = tmp_path / "push"
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

    _git(["remote", "add", "origin", str(gitlab_root / "proj.git")], cwd=work)
    _git(["push", "origin", TARGET_BRANCH, SOURCE_BRANCH], cwd=work)
    return {
        "gitlab_url": gitlab_root.as_uri(),  # file:// — no network
        "target_project": "proj",
        "main_sha": main_sha,
        "head_sha": head_sha,
    }


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
    """A fake opencode executable on PATH, switched by FAKE_OPENCODE_MODE."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(
        "#!/bin/sh\n"
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
        "  sleep)\n"
        "    exec sleep 30\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _run_entrypoint(tmp_path, gitlab, fake_opencode, mode, *, timeout="2", extra_projects=None):
    """Run the entrypoint with a controlled env (local file:// git URL)."""
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
        "MR_IID": "7",
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
        "WORK_DIR": str(tmp_path / "work"),
        "FAKE_OPENCODE_MODE": mode,
    }
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
    }
    assert config["permission"] == {"bash": "allow", "edit": "allow", "webfetch": "allow"}


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


def test_entrypoint_timeout(tmp_path, gitlab, fake_opencode):
    proc = _run_entrypoint(tmp_path, gitlab, fake_opencode, "sleep", timeout="1")
    assert proc.returncode == 124, proc.stdout + proc.stderr

    out_dir = tmp_path / "out"
    log_text = (out_dir / "review.log").read_text(encoding="utf-8")
    assert "TIMED OUT" in log_text
    assert log_text.rstrip("\n").endswith("EXIT=124")
    assert not (out_dir / "result.json").exists()
    assert not (out_dir / "result.md").exists()
