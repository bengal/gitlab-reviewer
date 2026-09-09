"""Assemble the per-run review prompt: base prompt + run context + output contract.

The base text is ``job.prompt_override`` when set, else the settings'
``default_review_prompt``. Context (project, MR, extra library repos) and the
strict single-fenced-```json output instruction are always appended.
"""

from app.models import MergeRequest, ScheduledJob, SettingsRow
from app.services.extra_projects import normalize_extra_projects

#: Shape of the required fenced JSON block (mirrors app.schemas.result).
RESULT_JSON_SCHEMA = """{
  "summary": "short overall assessment (2-5 sentences)",
  "findings": {
    "critical": [{"file": "path/to/file", "line": 123, "description": "what is wrong and why"}],
    "important": [{"file": "path/to/file", "line": 123, "description": "what is wrong and why"}],
    "minor": [{"file": "path/to/file", "line": 123, "description": "nit / style / readability"}],
    "positive": [{"file": "path/to/file", "line": 123, "description": "what is done well"}]
  },
  "commit_message_review": "assessment of the commit message(s), or empty string",
  "questions": ["open questions for the author"]
}"""


def _load_mr(job: ScheduledJob) -> MergeRequest:
    from app.db import get_db_session

    with get_db_session() as db:
        mr = db.get(MergeRequest, job.merge_request_id)
    if mr is None:
        raise ValueError(f"merge request {job.merge_request_id} not found")
    return mr


def _context_lines(job: ScheduledJob, settings: SettingsRow, mr: MergeRequest) -> list[str]:
    lines = [
        "## Review context",
        f"- Target project: {settings.gitlab_project}",
        f"- MR !{mr.iid}: {mr.title}",
        f"- Source branch: {mr.source_branch}",
        f"- Target branch: {mr.target_branch}",
        f"- Head commit: {mr.sha}",
    ]
    # Normalize like the container env and the library checkouts, so the
    # path the prompt promises is the path the run actually mounts: a
    # URL-only entry is listed at its derived /work/lib path, and an entry
    # without a url is omitted (it is never mounted).
    for entry in normalize_extra_projects(job.extra_projects or []):
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        ref = entry.get("ref") or "default branch"
        mount = f"/work/lib/{entry.get('path') or ''}".rstrip("/")
        lines.append(f"- Extra library repo (read-only): {url} @ {ref} mounted at {mount}")
    return lines


def _output_contract_lines() -> list[str]:
    return [
        "## Output requirements (strict)",
        "Your final answer MUST end with exactly one fenced ```json block matching",
        "this schema — no text after it, no additional fenced json blocks:",
        "```json",
        RESULT_JSON_SCHEMA,
        "```",
    ]


def build_review_prompt(
    job: ScheduledJob,
    settings: SettingsRow,
    *,
    mr: MergeRequest | None = None,
) -> str:
    """Full prompt text passed to the review container via REVIEW_PROMPT."""
    if mr is None:
        mr = _load_mr(job)
    base = (job.prompt_override or settings.default_review_prompt or "").strip()
    sections = []
    if base:
        sections.append(base)
    sections.append("\n".join(_context_lines(job, settings, mr)))
    sections.append("\n".join(_output_contract_lines()))
    return "\n\n".join(sections)
