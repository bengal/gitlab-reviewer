"""ReviewResult — tolerant parsing of the LLM-emitted result JSON.

The LLM is asked to emit a single fenced ```json block with this shape:

    {
      "summary": str,
      "findings": {"critical": [], "important": [], "minor": [], "positive": []},
      "commit_message_review": str,
      "questions": [str, ...]
    }

Each finding is {"file": str, "line": int|null, "description": str}. The
validators below are deliberately tolerant: LLMs misspell keys, nest things
unexpectedly or emit numbers as strings. Missing keys get defaults, wrong
types are coerced, and unparseable garbage degrades to an empty result
rather than raising (a run with unparseable JSON is still a success with
result_json=null at the service layer).
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

_INT_PREFIX = re.compile(r"(\d+)")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str = ""
    line: int | None = None
    description: str = ""

    @field_validator("file", "description", mode="before")
    @classmethod
    def _to_text(cls, value: Any) -> Any:
        return _coerce_text(value)

    @field_validator("line", mode="before")
    @classmethod
    def _to_int(cls, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        match = _INT_PREFIX.search(str(value))
        return int(match.group(1)) if match else None


class Findings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    critical: list[Finding] = []
    important: list[Finding] = []
    minor: list[Finding] = []
    positive: list[Finding] = []


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    findings: Findings = Findings()
    commit_message_review: str = ""
    questions: list[str] = []

    @field_validator("summary", "commit_message_review", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return _coerce_text(value)

    @field_validator("findings", mode="before")
    @classmethod
    def _findings(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, list):
            # Unbucketed list of findings: keep them, treat as "important".
            return {"important": value}
        if not isinstance(value, dict):
            return {}
        return value

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (dict, list, tuple)):
            return [_coerce_text(item) for item in value if item is not None]
        return [str(value)]


def parse_review_result(data: Any) -> ReviewResult:
    """Parse untrusted LLM JSON into a ReviewResult; never raises.

    Non-dict input (e.g. raw prose accidentally captured as the payload)
    becomes a result whose summary is that text.
    """
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ReviewResult()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ReviewResult(summary=text)
    if not isinstance(data, dict):
        return ReviewResult(summary=_coerce_text(data))
    try:
        return ReviewResult.model_validate(data)
    except Exception:
        return ReviewResult(summary=_coerce_text(data))
