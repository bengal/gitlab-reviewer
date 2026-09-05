"""Render a captured review outcome (result_json or raw markdown) to markdown.

One renderer serves both consumers of the review outcome (M6b):

- the GitLab post-back (``app.services.review_service``) posts the returned
  markdown as an MR note;
- the results detail page (``app.routers.results``) shows the same markdown
  so the user can see exactly what was/is posted.

Structured results are rendered in the shape the review prompt asks for:
Summary; findings grouped by severity (each finding as
``[file:line] description``, tolerating missing file/line); Commit Message
Review; Questions. When no structured result was captured, the raw markdown
from the runner is returned as-is.
"""

from app.schemas.result import Finding, parse_review_result

# (field on the parsed findings, section title), in display order.
SEVERITY_SECTIONS = (
    ("critical", "Critical"),
    ("important", "Important"),
    ("minor", "Minor"),
    ("positive", "Positive"),
)


def finding_ref(finding: Finding) -> str:
    """Locator for one finding: ``[file:line]``, ``[file]`` or ``[line N]``.

    Returns "" when both file and line are missing, so the caller can render
    the bare description.
    """
    file = (finding.file or "").strip()
    line = finding.line
    if file and line is not None:
        return f"[{file}:{line}]"
    if file:
        return f"[{file}]"
    if line is not None:
        return f"[line {line}]"
    return ""


def _finding_text(finding: Finding) -> str:
    ref = finding_ref(finding)
    description = (finding.description or "").strip()
    if ref and description:
        return f"{ref} {description}"
    return description or ref


def render_result_markdown(result_json, raw_markdown, run) -> str:
    """Render a run's outcome to markdown (detail page + GitLab note).

    ``result_json`` wins when present (and non-empty); a null/empty one falls
    back to ``raw_markdown``; when neither was captured a short note is
    returned, including the run's error message when it has one.
    """
    if result_json:
        result = parse_review_result(result_json)
        sections: list[str] = []
        if result.summary.strip():
            sections.append(f"## Summary\n\n{result.summary.strip()}")
        for key, title in SEVERITY_SECTIONS:
            findings = getattr(result.findings, key)
            if findings:
                lines = "\n".join(f"- {_finding_text(f)}" for f in findings)
                sections.append(f"## {title}\n\n{lines}")
        if result.commit_message_review.strip():
            sections.append(f"## Commit Message Review\n\n{result.commit_message_review.strip()}")
        if result.questions:
            numbered = "\n".join(
                f"{i}. {q.strip()}" for i, q in enumerate(result.questions, 1) if q.strip()
            )
            if numbered:
                sections.append(f"## Questions\n\n{numbered}")
        if sections:
            return "\n\n".join(sections) + "\n"
    if raw_markdown and raw_markdown.strip():
        return raw_markdown.strip() + "\n"
    note = "Review finished without a structured result."
    if run is not None and getattr(run, "error_message", None):
        note += f"\n\nError: {run.error_message.strip()}"
    return note + "\n"
