"""Tests for list[T] structured output request and parsing helpers."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core._aipl import AIPLResponseHeaders
from ai_pipeline_core._llm_core._response_builder import _build_model_response
from ai_pipeline_core._llm_core.client import _response_format
from ai_pipeline_core._llm_core.model_response import StreamCompletion, TimingData
from ai_pipeline_core._llm_core.request import (
    AttemptRequest,
    LLMRequest,
    ListOf,
    ResponseSpec,
    get_list_item_type,
    is_list_output_type,
)
from ai_pipeline_core._llm_core.types import TokenUsage
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class TaskGroupModel(BaseModel):
    name: str
    priority: int


class Finding(BaseModel):
    text: str


class DictFinding(BaseModel):
    values: dict[str, int]


def test_is_list_output_type_accepts_list_basemodel() -> None:
    assert is_list_output_type(list[TaskGroupModel]) is True


def test_is_list_output_type_rejects_plain_basemodel() -> None:
    assert is_list_output_type(TaskGroupModel) is False


def test_is_list_output_type_rejects_non_model_lists() -> None:
    assert is_list_output_type(list[str]) is False
    assert is_list_output_type(list[int]) is False
    assert is_list_output_type(list) is False


def test_get_list_item_type_extracts_inner() -> None:
    assert get_list_item_type(list[TaskGroupModel]) is TaskGroupModel


def test_listof_response_format_uses_object_root_items() -> None:
    payload = _response_format(ResponseSpec(format=ListOf(TaskGroupModel)))

    assert isinstance(payload, dict)
    schema = payload["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["items"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["items"]["type"] == "array"


def test_plain_basemodel_response_format_uses_json_schema() -> None:
    payload = _response_format(ResponseSpec(format=Finding))

    assert payload["type"] == "json_schema"
    assert payload["json_schema"]["name"] == "Finding"
    assert payload["json_schema"]["strict"] is True
    schema = payload["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["text"]
    assert schema["additionalProperties"] is False


def test_listof_response_format_rejects_dynamic_dict_schema() -> None:
    with pytest.raises(TypeError, match="dict type"):
        _response_format(ResponseSpec(format=ListOf(DictFinding)))


def test_build_model_response_unwraps_listof_items() -> None:
    items = [TaskGroupModel(name="a", priority=1), TaskGroupModel(name="b", priority=2)]
    content = json.dumps({"items": [item.model_dump(mode="json") for item in items]})
    completion = StreamCompletion(
        response=SimpleNamespace(
            id="resp_1",
            choices=(SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=()), finish_reason="stop"),),
        ),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        raw_headers={},
        aipl_headers=AIPLResponseHeaders(),
        timing=TimingData(started_at=0.0, first_token_at=0.1, finished_at=0.2),
        final_tps=10.0,
    )
    request = AttemptRequest(
        call=LLMRequest(
            model=DEFAULT_TEST_MODEL,
            messages=(),
            response=ResponseSpec(format=ListOf(TaskGroupModel)),
        ),
        model=DEFAULT_TEST_MODEL,
        attempt_index=0,
        call_id="call_1",
    )

    response = _build_model_response(completion, request, prompt_cache_key=None)

    assert isinstance(response.parsed, list)
    assert response.parsed == items
    assert json.loads(response.content) == [item.model_dump(mode="json") for item in items]


def test_model_response_serializes_list_of_models() -> None:
    from ai_pipeline_core._llm_core.model_response import ModelResponse

    items = [Finding(text="found it")]
    response = ModelResponse[Any](
        content="[]",
        parsed=items,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="test",
    )

    assert response.model_dump(mode="json")["parsed"] == [{"text": "found it"}]
