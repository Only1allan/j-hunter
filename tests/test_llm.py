"""JSON extraction from SiliconFlow model responses.

The model wraps JSON in markdown code fences or surrounds it with prose.
_extract_json must handle all these cases so structured output doesn't
silently fail.
"""

import asyncio

import pytest
from pydantic import BaseModel

from src import llm as llm_module
from src.llm import SiliconFlowClient


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


# --- schema conformance retry ------------------------------------------------


class _Shape(BaseModel):
    headline: str
    summary: str


def _fake_client(responses):
    """A client whose _chat returns canned bodies in order."""
    client = SiliconFlowClient(api_key="test")
    seen = []

    async def fake_chat(*, stable_system, user, max_tokens, json_schema=None):
        seen.append(user)
        body = responses[min(len(seen) - 1, len(responses) - 1)]
        return {"choices": [{"message": {"content": body}, "finish_reason": "stop"}]}

    client._chat = fake_chat
    return client, seen


def test_incomplete_response_is_retried():
    """This provider has no native structured-output mode, so a response with a
    required field missing is expected rather than exceptional. Before the retry,
    one such response discarded a whole package and the six calls spent on it."""
    client, seen = _fake_client([
        '{"headline":"x"}',                      # missing `summary`
        '{"headline":"x","summary":"y"}',
    ])
    result = asyncio.run(
        client.extract(stable_system="s", user="do it", schema=_Shape)
    )
    assert result.summary == "y"
    assert len(seen) == 2


def test_retry_prompt_names_the_missing_field():
    """Simply asking again is much less effective than saying what was wrong."""
    client, seen = _fake_client([
        '{"headline":"x"}',
        '{"headline":"x","summary":"y"}',
    ])
    asyncio.run(client.extract(stable_system="s", user="do it", schema=_Shape))
    assert "PREVIOUS ATTEMPT WAS REJECTED" in seen[1]
    assert "summary" in seen[1]


def test_persistently_invalid_response_raises_after_the_attempt_budget():
    client, seen = _fake_client(['{"headline":"x"}'])
    with pytest.raises(RuntimeError, match="could not be extracted"):
        asyncio.run(client.extract(stable_system="s", user="do it", schema=_Shape))
    assert len(seen) == llm_module.SCHEMA_ATTEMPTS


def test_unparseable_json_is_also_retried():
    client, seen = _fake_client([
        "here you go: not json at all",
        '{"headline":"x","summary":"y"}',
    ])
    result = asyncio.run(
        client.extract(stable_system="s", user="do it", schema=_Shape)
    )
    assert result.headline == "x"
    assert len(seen) == 2
