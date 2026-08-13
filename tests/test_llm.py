"""JSON extraction from SiliconFlow model responses.

The model wraps JSON in markdown code fences or surrounds it with prose.
_extract_json must handle all these cases so structured output doesn't
silently fail.
"""

from src.llm import SiliconFlowClient
import pytest


def test_clean_json():
    assert SiliconFlowClient._extract_json('{"score": 80, "name": "test"}') == {
        "score": 80, "name": "test"
    }


def test_json_in_code_fence():
    content = '```json\n{"score": 80, "name": "test"}\n```'
    assert SiliconFlowClient._extract_json(content) == {"score": 80, "name": "test"}


def test_json_in_plain_fence():
    content = '```\n{"score": 80}\n```'
    assert SiliconFlowClient._extract_json(content) == {"score": 80}


def test_json_with_prose_around_it():
    content = 'Here is my assessment:\n{"score": 80, "rationale": "good"}\nThat is all.'
    assert SiliconFlowClient._extract_json(content) == {"score": 80, "rationale": "good"}


def test_nested_json():
    content = '{"score": 80, "strengths": ["a", "b"], "gaps": []}'
    result = SiliconFlowClient._extract_json(content)
    assert result["score"] == 80
    assert result["strengths"] == ["a", "b"]
    assert result["gaps"] == []


def test_invalid_json_raises():
    with pytest.raises(RuntimeError, match="Could not parse JSON"):
        SiliconFlowClient._extract_json("this is not json at all")


def test_empty_string_raises():
    with pytest.raises(RuntimeError, match="Could not parse JSON"):
        SiliconFlowClient._extract_json("")


def test_json_with_extra_whitespace():
    content = '\n\n  {"score": 80}  \n\n'
    assert SiliconFlowClient._extract_json(content) == {"score": 80}
