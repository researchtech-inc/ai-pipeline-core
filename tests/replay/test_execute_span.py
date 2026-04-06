"""Tests for generic replay execution."""

from typing import Any

import pytest

from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core._llm_core import CoreMessage, Role
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline import PipelineTask
from ai_pipeline_core.pipeline._execution_context import get_execution_context
from ai_pipeline_core.replay import execute_span
from tests.replay.conftest import ReplayFlowOptions, ReplayResultDocument, ReplayTextDocument, make_span, store_document_in_database
from tests.support.helpers import create_test_model_response

_SEEN_CONTEXT: dict[str, Any] = {}


async def execute_function(*, value: str) -> str:
    """Replay target for bare function execution."""
    execution_ctx = get_execution_context()
    assert execution_ctx is not None
    _SEEN_CONTEXT["run_id"] = execution_ctx.run_id
    _SEEN_CONTEXT["publisher_type"] = type(execution_ctx.publisher)
    _SEEN_CONTEXT["disable_cache"] = execution_ctx.disable_cache
    return f"function:{value}"


class ExecuteClassMethod:
    """Replay target for classmethod execution."""

    @classmethod
    async def run(cls, value: str) -> str:
        return f"{cls.__name__}:{value}"


class ExecuteInstanceMethod:
    """Replay target for instance-method execution."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    async def run(self, replay_docs: tuple[ReplayTextDocument, ...], options: ReplayFlowOptions) -> str:
        return f"{self.prefix}:{replay_docs[0].name}:{options.replay_label}"


class ExecuteCacheableTask(PipelineTask):
    """Replay target for cacheable task execution."""

    cacheable = True
    cache_ttl_seconds = 60

    @classmethod
    async def run(cls, source: ReplayTextDocument) -> tuple[ReplayResultDocument, ...]:
        _ = cls
        return (ReplayResultDocument(name="cached.txt", content=source.content, description="live"),)


@pytest.mark.asyncio
async def test_execute_span_replays_all_adapter_kinds(
    monkeypatch: pytest.MonkeyPatch,
    memory_database,
    sample_text_doc: ReplayTextDocument,
) -> None:
    await store_document_in_database(memory_database, sample_text_doc)

    async def fake_generate(messages: Any, **kwargs: Any) -> Any:
        _ = (messages, kwargs)
        return create_test_model_response(content="decoded response")

    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", fake_generate)

    function_span = make_span(
        kind="task",
        name="function",
        target=f"function:{__name__}:execute_function",
        input_value={"value": "ok"},
    )
    classmethod_span = make_span(
        kind="task",
        name="classmethod",
        target=f"classmethod:{__name__}:ExecuteClassMethod.run",
        input_value={"value": "ok"},
    )
    instance_span = make_span(
        kind="flow",
        name="instance",
        target=f"instance_method:{__name__}:ExecuteInstanceMethod.run",
        receiver_mode="constructor_args",
        receiver_value={"prefix": "instance"},
        input_value={"replay_docs": (sample_text_doc,), "options": ReplayFlowOptions(replay_label="deep")},
    )
    decoded_span = make_span(
        kind="conversation",
        name="decoded",
        target="decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        receiver_mode="decoded_state",
        receiver_value=Conversation(model="test-model"),
        input_value={
            "content": "hello",
            "tools": (),
            "tool_choice": None,
            "max_tool_rounds": 1,
            "purpose": "decoded",
            "expected_cost": None,
            "response_format": None,
        },
    )

    for span in (function_span, classmethod_span, instance_span, decoded_span):
        await memory_database.insert_span(span)

    assert await execute_span(function_span.span_id, source_db=memory_database) == "function:ok"
    assert await execute_span(classmethod_span.span_id, source_db=memory_database) == "ExecuteClassMethod:ok"
    assert await execute_span(instance_span.span_id, source_db=memory_database) == "instance:notes.txt:deep"
    decoded_result = await execute_span(decoded_span.span_id, source_db=memory_database)
    assert decoded_result.content == "decoded response"


@pytest.mark.asyncio
async def test_execute_span_installs_replay_execution_context(memory_database) -> None:
    span = make_span(
        kind="task",
        name="function",
        target=f"function:{__name__}:execute_function",
        input_value={"value": "context"},
    )
    await memory_database.insert_span(span)

    result = await execute_span(span.span_id, source_db=memory_database)

    assert result == "function:context"
    assert _SEEN_CONTEXT["run_id"].startswith(f"replay-{str(span.span_id)[:8]}-")
    assert _SEEN_CONTEXT["publisher_type"] is _NoopPublisher
    assert _SEEN_CONTEXT["disable_cache"] is True


@pytest.mark.asyncio
async def test_execute_span_disables_pipeline_task_cache_reads(
    monkeypatch: pytest.MonkeyPatch,
    memory_database,
    sample_text_doc: ReplayTextDocument,
) -> None:
    await store_document_in_database(memory_database, sample_text_doc)
    span = make_span(
        kind="task",
        name="cacheable",
        target=f"classmethod:{__name__}:ExecuteCacheableTask.run",
        input_value={"source": sample_text_doc},
    )
    await memory_database.insert_span(span)

    async def fail_cached_completion(cache_key: str, max_age: Any) -> Any:
        _ = (cache_key, max_age)
        raise AssertionError("replay should not read PipelineTask cache")

    monkeypatch.setattr(memory_database, "get_cached_completion", fail_cached_completion)

    result = await execute_span(span.span_id, source_db=memory_database, sink_db=memory_database)

    assert len(result) == 1
    assert result[0].description == "live"


@pytest.mark.asyncio
async def test_execute_span_replays_llm_round_boundary(
    monkeypatch: pytest.MonkeyPatch,
    memory_database,
) -> None:
    async def fake_generate(messages: Any, **kwargs: Any) -> Any:
        _ = (messages, kwargs)
        return create_test_model_response(content="llm-round replayed")

    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", fake_generate)
    span = make_span(
        kind="llm_round",
        name="round-1",
        target="function:ai_pipeline_core.llm.conversation:_replay_llm_round",
        input_value={
            "messages": [CoreMessage(role=Role.USER, content="hello")],
            "model": "test-model",
            "model_options": None,
            "tool_choice": None,
            "response_format": None,
            "purpose": "llm-round-replay",
            "expected_cost": None,
            "round_index": 1,
            "tool_schemas": [],
        },
    )
    await memory_database.insert_span(span)

    result = await execute_span(span.span_id, source_db=memory_database)
    assert result.content == "llm-round replayed"
