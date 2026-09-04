"""Tolerant parsing of untrusted LLM result JSON."""

from app.schemas import ReviewResult, parse_review_result

FULL = {
    "summary": "LGTM overall",
    "findings": {
        "critical": [{"file": "a.c", "line": 10, "description": "use-after-free"}],
        "important": [],
        "minor": [{"file": "b.c", "line": "42", "description": "style"}],
        "positive": [{"description": "nice refactor"}],
    },
    "commit_message_review": "subject could be shorter",
    "questions": ["Is this intentional?"],
}


def test_full_shape():
    result = parse_review_result(FULL)
    assert result.summary == "LGTM overall"
    assert result.findings.critical[0].file == "a.c"
    assert result.findings.critical[0].line == 10
    assert result.findings.minor[0].line == 42  # "42" coerced
    assert result.findings.positive[0].file == ""
    assert result.commit_message_review == "subject could be shorter"
    assert result.questions == ["Is this intentional?"]


def test_missing_keys_default():
    result = parse_review_result({})
    assert result.summary == ""
    assert result.findings.critical == []
    assert result.questions == []


def test_round_trip_model_validate():
    result = ReviewResult.model_validate(FULL)
    assert result == parse_review_result(FULL)


def test_lenient_garbage():
    assert parse_review_result("just some prose").summary == "just some prose"
    assert parse_review_result(None).summary == ""
    assert parse_review_result({"summary": 42, "questions": "one question"}).summary == "42"
    assert parse_review_result({"questions": "one question"}).questions == ["one question"]
    unbucketed = parse_review_result({"findings": [{"file": "x", "line": 1, "description": "d"}]})
    assert unbucketed.findings.important
    assert parse_review_result({"findings": "nonsense"}).findings.important == []
    assert parse_review_result('{"summary": "from a json string"}').summary == "from a json string"
    assert parse_review_result("not json {").summary == "not json {"
