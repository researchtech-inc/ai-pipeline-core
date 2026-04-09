"""Unit tests for llm/_tool_loop.py."""

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar
from uuid import uuid7

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.llm._tool_loop import _execute_single_tool, execute_tool_loop
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings

from .conftest import make_response, make_tool_call


class SearchTool(Tool):
    """Search the web."""

    class Input(BaseModel):
        query: str = Field(description="Search query")

    class Output(BaseModel):
        results: str

    async def run(self, input: Input) -> Output:
        return self.Output(results=f"Results for: {input.query}")


class FailingTool(Tool):
    """A tool that always raises."""

    class Input(BaseModel):
        reason: str = Field(description="Failure reason")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        raise RuntimeError(f"Intentional: {input.reason}")


class SlowTool(Tool):
    """Tool that takes too long."""

    class Input(BaseModel):
        delay: float = Field(description="Delay in seconds")

    timeout_seconds: ClassVar[int] = 2

    class Output(BaseModel):
        status: str

    async def run(self, input: Input) -> Output:
        import asyncio

        await asyncio.sleep(input.delay)
        return self.Output(status="done")


class FastTimeoutSlowTool(SlowTool):
    """SlowTool with a very short timeout for testing."""

    timeout_seconds: ClassVar[int] = 1


class _StatefulTool(Tool):
    """Tool with init state for testing no-API-key-leakage."""

    class Input(BaseModel):
        q: str = Field(description="q")

    class Output(BaseModel):
        result: str

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def run(self, input: Input) -> Output:
        return self.Output(result="ok")


@dataclass(frozen=True)
class _FakeToolResultMsg:
    tool_call_id: str
    function_name: str
    content: str


def _build_msg(tid: str, fn: str, content: str) -> _FakeToolResultMsg:
    return _FakeToolResultMsg(tool_call_id=tid, function_name=fn, content=content)


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]


def _make_context(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    span_id = uuid7()
    return ExecutionContext(
        run_id="tool-loop-test",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="tool-loop-test",
        span_id=span_id,
        current_span_id=span_id,
        flow_span_id=span_id,
    )


def _finished_spans(database: _RecordingSpanDatabase, kind: str) -> list[object]:
    return [span for span in database.inserted_spans if getattr(span, "kind", None) == kind and getattr(span, "output_json", "")]


async def test_execute_single_tool_success_returns_record_output_and_span() -> None:
    tool = SearchTool()
    tc = make_tool_call("c1", "search", '{"query": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is not None
    assert record.tool is SearchTool
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    tool_meta = json.loads(tool_call_span.meta_json)
    assert tool_meta["tool_name"] == "search_tool"
    assert tool_meta["round_index"] == 1
    assert json.loads(output.content)["results"] == "Results for: test"


async def test_execute_single_tool_validation_error_records_failed_tool_call() -> None:
    tool = SearchTool()
    tc = make_tool_call("c1", "search", "not valid json")
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is None
    assert "Invalid arguments" in output.content
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    assert tool_call_span.error_type == ""
    tool_meta = json.loads(tool_call_span.meta_json)
    assert tool_meta["tool_call_id"] == "c1"
    assert tool_meta["round_index"] == 1


async def test_execute_single_tool_timeout_records_failed_tool_call() -> None:
    database = _RecordingSpanDatabase()
    tool = FastTimeoutSlowTool()
    tc = make_tool_call("c1", "fast_timeout_slow_tool", '{"delay": 10}')
    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=2)

    assert record is not None
    assert "timed out" in output.content
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    assert tool_call_span.error_type == ""
    assert json.loads(tool_call_span.meta_json)["round_index"] == 2


async def test_execute_tool_loop_records_rounds_and_tool_calls_in_order() -> None:
    call_count = 0

    async def invoke_llm(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_response(
                content="",
                tool_calls=(make_tool_call("c1", "search", '{"query": "test"}'),),
            )
        return make_response(content="Found results")

    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context(database)):
        msgs, resp, records = await execute_tool_loop(
            invoke_llm=invoke_llm,
            tool_schemas=[{"type": "function", "function": {"name": "search"}}],
            tool_lookup={"search": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=5,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="search for test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert resp.content == "Found results"
    assert len(records) == 1
    assert len(msgs) == 3
    assert [span.kind for span in _finished_spans(database, SpanKind.TOOL_CALL)] == [SpanKind.TOOL_CALL]


async def test_execute_tool_loop_unknown_tool_records_tool_call_span() -> None:
    call_count = 0

    async def invoke_llm(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal call_count
        _ = kwargs
        call_count += 1
        return make_response(content="", tool_calls=(make_tool_call("c1", "missing_tool", "{}"),)) if call_count == 1 else make_response(content="done")

    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context(database)):
        msgs, resp, records = await execute_tool_loop(
            invoke_llm=invoke_llm,
            tool_schemas=[],
            tool_lookup={"search": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=5,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="hi")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert resp.content == "done"
    assert records == ()
    assert any("Unknown tool" in msg.content for msg in msgs if isinstance(msg, _FakeToolResultMsg))


# ── Phase 7c: Tool loop span metadata ───────────────────────────────────────


async def test_execute_single_tool_uses_name_classvar() -> None:
    """Span metadata uses tool_cls.name (ClassVar) instead of to_snake_case()."""
    tool = SearchTool()
    tc = make_tool_call("c1", "search", '{"query": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        await _execute_single_tool(tool, tc, round_num=1)

    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    tool_meta = json.loads(tool_call_span.meta_json)
    assert tool_meta["tool_name"] == "search_tool"  # from name ClassVar


async def test_execute_single_tool_span_target_tool_call_prefix() -> None:
    """Span target uses 'tool_call:module:qualname'."""
    tool = SearchTool()
    tc = make_tool_call("c1", "search", '{"query": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        await _execute_single_tool(tool, tc, round_num=1)

    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    assert tool_call_span.target.startswith("tool_call:")
    assert "SearchTool" in tool_call_span.target


async def test_execute_single_tool_receiver_tool_ref_mode() -> None:
    """Span receiver payload uses mode='tool_ref' with name and class_path only."""
    tool = SearchTool()
    tc = make_tool_call("c1", "search", '{"query": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        await _execute_single_tool(tool, tc, round_num=1)

    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    receiver = json.loads(tool_call_span.receiver_json)
    assert receiver["mode"] == "tool_ref"
    assert receiver["value"]["name"] == "search_tool"
    assert "class_path" in receiver["value"]


async def test_execute_single_tool_no_constructor_args_in_span() -> None:
    """Span receiver payload does not contain constructor_args."""
    tool = _StatefulTool(api_key="secret-key-123")
    tc = make_tool_call("c1", "stateful_tool", '{"q": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        await _execute_single_tool(tool, tc, round_num=1)

    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    receiver = json.loads(tool_call_span.receiver_json)
    assert "constructor_args" not in receiver
    assert "secret-key-123" not in tool_call_span.receiver_json


async def test_execute_single_tool_unhandled_error_caught_by_tool_loop() -> None:
    """Unhandled exceptions from run() propagate through execute() to tool loop's except block."""
    tool = FailingTool()
    tc = make_tool_call("c1", "failing_tool", '{"reason": "crash"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is not None
    assert "failed" in output.content or "Intentional" in output.content
