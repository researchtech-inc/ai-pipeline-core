"""Tests for task-scoped conversation span recording."""

# pyright: reportPrivateUsage=false

import asyncio
import json
import logging
from types import MappingProxyType
from uuid import uuid7

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core import ModelOptions
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline import PipelineTask
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class _RecorderInputDoc(Document):
    """Input document for recorder tests."""


class _RecorderOutputDoc(Document):
    """Output document for recorder tests."""


class SearchTool(Tool):
    """Search tool for recorder tests."""

    class Input(BaseModel):
        query: str = Field(description="Search query")

    class Output(BaseModel):
        results: str

    async def run(self, input: Input) -> Output:
        return self.Output(results=f"Results for {input.query}")


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]


class _ConversationInsertFailureDatabase(_RecordingSpanDatabase):
    async def insert_span(self, span: object) -> None:
        if getattr(span, "kind", None) == SpanKind.CONVERSATION:
            raise RuntimeError("conversation insert failed")
        await super().insert_span(span)


def _make_response(
    *,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    cost: float = 0.0,
    response_id: str = "resp-test",
    tool_calls: tuple[object, ...] = (),
) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
        cost=cost,
        model="test-model",
        response_id=response_id,
        metadata={"time_taken": 0.2, "first_token_time": 0.1},
        citations=(),
        reasoning_content="reasoning",
        tool_calls=tool_calls,  # type: ignore[arg-type]
    )


def _make_fake_generate(results: list[ModelResponse[str] | BaseException]):
    remaining = list(results)

    async def _fake_generate(*args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        next_item = remaining.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item

    return _fake_generate


def _make_context_with_db(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    flow_span_id = uuid7()
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="test-pipeline",
        flow_frame=FlowFrame(
            name="RecorderFlow",
            flow_class_name="RecorderFlow",
            step=1,
            total_steps=1,
            flow_minutes=(1.0,),
            completed_minutes=0.0,
            flow_params={},
        ),
        span_id=flow_span_id,
        current_span_id=flow_span_id,
        flow_span_id=flow_span_id,
    )


class _SingleSendTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("hello", purpose="single-send")
        return (_RecorderOutputDoc.derive(derived_from=(input_docs[0],), name="single.txt", content=conv.content),)


class _ThreeTurnTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("first", purpose="first-turn")
        conv = await conv.send("second", purpose="second-turn")
        conv = await conv.send("third", purpose="third-turn")
        return (_RecorderOutputDoc.derive(derived_from=(input_docs[0],), name="multi.txt", content=conv.content),)


class _WarmupForkTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        base = Conversation(model="test-model", enable_substitutor=False)
        warmup = await base.send("warmup", purpose="warmup")
        branch_a, branch_b = await asyncio.gather(
            warmup.send("branch-a", purpose="branch-a"),
            warmup.send("branch-b", purpose="branch-b"),
        )
        return (_RecorderOutputDoc.derive(derived_from=(input_docs[0],), name="fork.txt", content=f"{branch_a.content}|{branch_b.content}"),)


class _ToolTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("search", purpose="tool-task", tools=[SearchTool()])
        return (_RecorderOutputDoc.derive(derived_from=(input_docs[0],), name="tool.txt", content=conv.content),)


class _RecorderFailureTask(PipelineTask):
    last_conversation_id = "unset"

    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("hello", purpose="recorder-failure")
        cls.last_conversation_id = conv._conversation_id
        return (_RecorderOutputDoc.derive(derived_from=(input_docs[0],), name="failure.txt", content=conv.content),)


class _FailedSendTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        _ = input_docs
        conv = Conversation(model="test-model", enable_substitutor=False)
        await conv.send("boom", purpose="failed-send")
        return ()


def _make_input() -> _RecorderInputDoc:
    return _RecorderInputDoc.create_root(name="input.txt", content="input", reason="recorder-test")


class _OptionsTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(
            model="test-model",
            enable_substitutor=False,
            include_date=False,
            model_options=ModelOptions(reasoning_effort="medium"),
        )
        conv = await conv.send("hello", purpose="options-test")
        return (_RecorderOutputDoc.derive(derived_from=input_docs, name="options.txt", content=conv.content),)


class _DatePromptTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(
            model="test-model",
            enable_substitutor=False,
            current_date="2026-03-12",
            model_options=ModelOptions(system_prompt="Be precise."),
        )
        conv = await conv.send("hello", purpose="date-test")
        return (_RecorderOutputDoc.derive(derived_from=input_docs, name="date.txt", content=conv.content),)


class _StartedSpanTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_RecorderInputDoc, ...]) -> tuple[_RecorderOutputDoc, ...]:
        conv = Conversation(
            model="test-model",
            enable_substitutor=False,
            current_date="2026-03-12",
            model_options=ModelOptions(reasoning_effort="medium", system_prompt="Base prompt."),
        )
        conv = await conv.send(
            "hello",
            purpose="started-span",
            tools=[SearchTool()],
            tool_choice="auto",
            max_tool_rounds=4,
        )
        return (_RecorderOutputDoc.derive(derived_from=input_docs, name="started.txt", content=conv.content),)


def _spans(database: _MemoryDatabase, kind: str) -> list[object]:
    return sorted(
        (span for span in database._spans.values() if span.kind == kind),
        key=lambda span: (span.sequence_no, str(span.span_id)),
    )


@pytest.mark.asyncio
async def test_single_send_records_conversation_and_llm_round_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="single", prompt_tokens=11, completion_tokens=7, cached_tokens=5, reasoning_tokens=3, cost=0.25)]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _SingleSendTask.run((_make_input(),))

    conversation_span = _spans(database, SpanKind.CONVERSATION)[0]
    llm_round_span = _spans(database, SpanKind.LLM_ROUND)[0]
    conversation_meta = json.loads(conversation_span.meta_json)
    llm_meta = json.loads(llm_round_span.meta_json)
    llm_metrics = json.loads(llm_round_span.metrics_json)

    assert conversation_span.status == SpanStatus.COMPLETED
    assert conversation_span.previous_conversation_id is None
    assert conversation_span.cost_usd == 0.0
    assert conversation_meta["purpose"] == "single-send"
    assert llm_metrics["tokens_input"] == 11
    assert llm_metrics["tokens_output"] == 7
    assert llm_metrics["tokens_cache_read"] == 5
    assert llm_metrics["tokens_reasoning"] == 3
    assert llm_round_span.parent_span_id == conversation_span.span_id
    assert llm_round_span.cost_usd == 0.25
    assert llm_meta["round_index"] == 1
    assert llm_meta["request_messages"][-1]["content"] == "hello"
    assert llm_meta["response_content"] == "single"


@pytest.mark.asyncio
async def test_single_send_records_replayable_llm_round_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="single", prompt_tokens=11, completion_tokens=7, cost=0.25)]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _SingleSendTask.run((_make_input(),))

    llm_round_span = _spans(database, SpanKind.LLM_ROUND)[0]
    assert llm_round_span.target == "function:ai_pipeline_core.llm.conversation:_replay_llm_round"


@pytest.mark.asyncio
async def test_multi_turn_chain_links_previous_conversation_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(content="first", prompt_tokens=10, completion_tokens=5, response_id="resp-1"),
            _make_response(content="second", prompt_tokens=12, completion_tokens=6, response_id="resp-2"),
            _make_response(content="third", prompt_tokens=14, completion_tokens=7, response_id="resp-3"),
        ]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _ThreeTurnTask.run((_make_input(),))

    first, second, third = _spans(database, SpanKind.CONVERSATION)
    assert first.previous_conversation_id is None
    assert second.previous_conversation_id == first.span_id
    assert third.previous_conversation_id == second.span_id


@pytest.mark.asyncio
async def test_conversation_terminal_row_preserves_started_sequence_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(content="first", prompt_tokens=10, completion_tokens=5, response_id="resp-1"),
            _make_response(content="second", prompt_tokens=12, completion_tokens=6, response_id="resp-2"),
            _make_response(content="third", prompt_tokens=14, completion_tokens=7, response_id="resp-3"),
        ]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _ThreeTurnTask.run((_make_input(),))

    by_span_id: dict[object, list[object]] = {}
    for span in database.inserted_spans:
        if getattr(span, "kind", None) != SpanKind.CONVERSATION:
            continue
        by_span_id.setdefault(span.span_id, []).append(span)

    for rows in by_span_id.values():
        assert len(rows) == 2
        started_row = next(row for row in rows if row.status == SpanStatus.RUNNING)
        terminal_row = next(row for row in rows if row.status == SpanStatus.COMPLETED)
        assert terminal_row.sequence_no == started_row.sequence_no


@pytest.mark.asyncio
async def test_conversation_sequence_numbers_do_not_skip_after_terminal_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(content="first", prompt_tokens=10, completion_tokens=5, response_id="resp-1"),
            _make_response(content="second", prompt_tokens=12, completion_tokens=6, response_id="resp-2"),
            _make_response(content="third", prompt_tokens=14, completion_tokens=7, response_id="resp-3"),
        ]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _ThreeTurnTask.run((_make_input(),))

    conversation_spans = _spans(database, SpanKind.CONVERSATION)
    sequence_numbers = [span.sequence_no for span in conversation_spans]
    assert sequence_numbers == list(range(sequence_numbers[0], sequence_numbers[0] + len(sequence_numbers)))


@pytest.mark.asyncio
async def test_warmup_fork_records_two_children_of_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(content="warmup", prompt_tokens=10, completion_tokens=5, response_id="resp-warm"),
            _make_response(content="branch-a", prompt_tokens=11, completion_tokens=6, response_id="resp-a"),
            _make_response(content="branch-b", prompt_tokens=12, completion_tokens=7, response_id="resp-b"),
        ]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _WarmupForkTask.run((_make_input(),))

    warmup, branch_a, branch_b = _spans(database, SpanKind.CONVERSATION)
    assert branch_a.previous_conversation_id == warmup.span_id
    assert branch_b.previous_conversation_id == warmup.span_id


@pytest.mark.asyncio
async def test_tool_loop_records_llm_round_and_tool_call_children(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.llm.conftest import make_tool_call

    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(
                content="",
                prompt_tokens=10,
                completion_tokens=1,
                response_id="resp-tool-1",
                tool_calls=(make_tool_call("call-1", "search_tool", '{"query": "search"}'),),
            ),
            _make_response(content="final", prompt_tokens=15, completion_tokens=4, cost=0.4, response_id="resp-tool-2"),
        ]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _ToolTask.run((_make_input(),))

    conversation_span = _spans(database, SpanKind.CONVERSATION)[0]
    llm_rounds = _spans(database, SpanKind.LLM_ROUND)
    tool_calls = _spans(database, SpanKind.TOOL_CALL)
    conversation_meta = json.loads(conversation_span.meta_json)
    tool_meta = json.loads(tool_calls[0].meta_json)

    assert len(llm_rounds) == 2
    assert len(tool_calls) == 1
    assert conversation_meta["tools"][0]["name"] == "search_tool"
    assert tool_calls[0].parent_span_id == conversation_span.span_id
    assert tool_calls[0].cost_usd == 0.0
    assert tool_meta["tool_name"] == "search_tool"
    assert tool_meta["tool_call_id"] == "call-1"
    assert llm_rounds[0].cost_usd == 0.0
    assert llm_rounds[1].cost_usd == 0.4


@pytest.mark.asyncio
async def test_database_sink_failure_drops_conversation_rows_but_keeps_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="ok", prompt_tokens=9, completion_tokens=4, cost=0.5)]),
    )
    database = _ConversationInsertFailureDatabase()
    _RecorderFailureTask.last_conversation_id = "unset"
    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.pipeline._span_sink")
    with set_execution_context(_make_context_with_db(database)):
        await _RecorderFailureTask.run((_make_input(),))

    assert _RecorderFailureTask.last_conversation_id not in {"", "unset"}
    assert _spans(database, SpanKind.CONVERSATION) == []
    assert "Database span insert failed for conversation" in caplog.text


@pytest.mark.asyncio
async def test_failed_send_records_failed_conversation_and_failed_llm_round(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([RuntimeError("llm boom")]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        with pytest.raises(RuntimeError, match="llm boom"):
            await _FailedSendTask.run((_make_input(),))

    conversation_span = _spans(database, SpanKind.CONVERSATION)[0]
    llm_round_span = _spans(database, SpanKind.LLM_ROUND)[0]

    assert conversation_span.status == SpanStatus.FAILED
    assert conversation_span.error_type == "RuntimeError"
    assert conversation_span.error_message == "llm boom"
    assert llm_round_span.status == SpanStatus.FAILED
    assert llm_round_span.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_conversation_detail_records_base_and_effective_model_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="single", prompt_tokens=11, completion_tokens=7)]),
    )
    database = _RecordingSpanDatabase()
    context = _make_context_with_db(database)

    with set_execution_context(context):
        await _OptionsTask.run((_make_input(),))

    conversation_span = _spans(database, SpanKind.CONVERSATION)[0]
    llm_round_span = _spans(database, SpanKind.LLM_ROUND)[0]
    conversation_meta = json.loads(conversation_span.meta_json)
    llm_input = json.loads(llm_round_span.input_json)
    llm_model_options = llm_input["model_options"]

    assert conversation_meta["model_options"]["reasoning_effort"] == "medium"
    assert conversation_meta["model_options"]["timeout"] == 600
    assert conversation_meta["model_options"]["retries"] is None
    assert conversation_meta["model_options"]["cache_ttl"] == "300s"
    assert conversation_meta["effective_model_options"]["reasoning_effort"] == "medium"
    assert conversation_meta["effective_model_options"]["timeout"] == 600
    assert conversation_meta["effective_model_options"]["retries"] is None
    assert conversation_meta["effective_model_options"]["cache_ttl"] == "300s"
    assert llm_model_options["$type"] == "pydantic"
    assert llm_model_options["data"]["reasoning_effort"] == "medium"
    assert llm_model_options["data"]["timeout"] == 600
    assert llm_model_options["data"]["retries"] is None
    assert llm_model_options["data"]["cache_ttl"] == "300s"


@pytest.mark.asyncio
async def test_conversation_detail_records_effective_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="single", prompt_tokens=11, completion_tokens=7)]),
    )
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context_with_db(database)):
        await _DatePromptTask.run((_make_input(),))

    conversation_span = _spans(database, SpanKind.CONVERSATION)[0]
    detail = json.loads(conversation_span.meta_json)

    assert detail["model_options"]["system_prompt"] == "Be precise."
    assert detail["effective_system_prompt"] == "Be precise.\n\nCurrent date: 2026-03-12"


@pytest.mark.asyncio
async def test_started_conversation_span_includes_crash_diagnostic_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response(content="tool result", prompt_tokens=11, completion_tokens=7)]),
    )
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context_with_db(database)):
        await _StartedSpanTask.run((_make_input(),))

    started_rows = [
        span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.CONVERSATION and getattr(span, "status", None) == SpanStatus.RUNNING
    ]
    assert len(started_rows) == 1
    input_payload = json.loads(started_rows[0].input_json)
    receiver_payload = json.loads(started_rows[0].receiver_json)

    assert receiver_payload["mode"] == "decoded_state"
    assert input_payload["tool_choice"] == "auto"
    assert input_payload["max_tool_rounds"] == 4
    assert input_payload["response_format"] is None
    assert input_payload["tools"]["items"][0]["name"] == "search_tool"
