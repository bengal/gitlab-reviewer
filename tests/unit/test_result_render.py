"""Unit tests for app.services.result_render (M6b).

The renderer serves both the GitLab post-back note and the results detail
page; these tests pin down its shape: section order, [file:line] finding
locators (tolerant of missing file/line), and the raw-markdown / empty
fallbacks.
"""

from app.models import ReviewRun
from app.schemas.result import Finding
from app.services.result_render import finding_ref, render_result_markdown


def _run(error_message: str | None = None) -> ReviewRun:
    return ReviewRun(
        scheduled_job_id=1,
        merge_request_id=1,
        model_profile_id=1,
        error_message=error_message,
    )


def test_finding_ref_tolerates_missing_file_and_line():
    def ref(file, line):
        return finding_ref(Finding(file=file, line=line, description="d"))

    assert ref("src/foo.c", 42) == "[src/foo.c:42]"
    assert ref("src/foo.c", None) == "[src/foo.c]"
    assert ref(None, 42) == "[line 42]"
    assert ref("", None) == ""


def test_renders_structured_result_sections_in_order():
    data = {
        "summary": "Overall looks good.",
        "findings": {
            "critical": [{"file": "a.c", "line": 1, "description": "use after free"}],
            "important": [{"description": "missing tests"}],
            "minor": [{"file": "b.c", "line": "9", "description": "style"}],
            "positive": [{"file": "README.md", "description": "docs updated"}],
        },
        "commit_message_review": "Commit message is fine.",
        "questions": ["Is this intentional?", ""],
    }
    md = render_result_markdown(data, "raw should be ignored", _run())

    assert md == (
        "## Summary\n\nOverall looks good.\n\n"
        "## Critical\n\n- [a.c:1] use after free\n\n"
        "## Important\n\n- missing tests\n\n"
        "## Minor\n\n- [b.c:9] style\n\n"
        "## Positive\n\n- [README.md] docs updated\n\n"
        "## Commit Message Review\n\nCommit message is fine.\n\n"
        "## Questions\n\n1. Is this intentional?\n"
    )


def test_omits_empty_sections():
    md = render_result_markdown({"summary": "Just a summary."}, None, _run())
    assert md == "## Summary\n\nJust a summary.\n"


def test_null_result_json_falls_back_to_raw_markdown():
    md = render_result_markdown(None, "Raw prose review.\n", _run())
    assert md == "Raw prose review.\n"


def test_empty_result_json_falls_back_to_raw_markdown():
    md = render_result_markdown({}, "Raw fallback.", _run())
    assert md == "Raw fallback.\n"


def test_nothing_captured_mentions_error_when_present():
    run = _run(error_message="opencode crashed")
    md = render_result_markdown(None, None, run)
    assert "without a structured result" in md
    assert "Error: opencode crashed" in md


def test_nothing_captured_without_error():
    md = render_result_markdown(None, "   ", _run())
    assert md == "Review finished without a structured result.\n"
