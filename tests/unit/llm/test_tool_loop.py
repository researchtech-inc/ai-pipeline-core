"""Unit tests for tool-related paths in llm._engine."""

import json
from types import MappingProxyType
from uuid import uuid7

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.llm import _engine
from ai_pipeline_core.llm._request_messages import ToolResultMessage
from ai_pipeline_core.llm._engine import InteractionRequest, ToolRuntime, _execute_single_tool, _tool_loop
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings

from tests.support.helpers import make_tool_call

from .conftest import make_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


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


class FastTimeoutSlowTool(Tool):
    """Slow tool with a very short timeout for testing."""

    timeout_seconds = 1

    class Input(BaseModel):
        delay: float = Field(description="Delay in seconds")

    class Output(BaseModel):
        status: str

    async def run(self, input: Input) -> Output:
        import asyncio

        await asyncio.sleep(input.delay)
        return self.Output(status="done")


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


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]  # negative test: wrong runtime type


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
    return [
        span
        for span in database.inserted_spans
        if getattr(span, "kind", None) == kind and getattr(span, "output_json", "")
    ]


def _request(*, max_rounds: int = 5) -> InteractionRequest:
    return InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="search for test"),),
        model=DEFAULT_TEST_MODEL,
        tools=ToolRuntime(
            schemas=({"type": "function", "function": {"name": "search_tool"}},),
            instances={"search_tool": SearchTool()},
            choice="auto",
            max_rounds=max_rounds,
        ),
    )


async def test_execute_single_tool_success_returns_record_output_and_span() -> None:
    tool = SearchTool()
    tc = make_tool_call("c1", "search_tool", '{"query": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is not None
    assert record.tool is SearchTool
    assert json.loads(output.content)["results"] == "Results for: test"
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    tool_meta = json.loads(tool_call_span.meta_json)
    assert tool_meta["tool_name"] == "search_tool"
    assert tool_meta["round_index"] == 1


async def test_execute_single_tool_validation_error_does_not_create_record() -> None:
    tool = SearchTool()
    tc = make_tool_call("c1", "search_tool", "not valid json")
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is None
    assert "Invalid arguments" in output.content
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    assert json.loads(tool_call_span.meta_json)["tool_call_id"] == "c1"


async def test_execute_single_tool_timeout_creates_record() -> None:
    database = _RecordingSpanDatabase()
    tool = FastTimeoutSlowTool()
    tc = make_tool_call("c1", "fast_timeout_slow_tool", '{"delay": 10}')

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=2)

    assert record is not None
    assert "timed out" in output.content
    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    assert json.loads(tool_call_span.meta_json)["round_index"] == 2


async def test_execute_single_tool_runtime_failure_creates_record() -> None:
    tool = FailingTool()
    tc = make_tool_call("c1", "failing_tool", '{"reason": "crash"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        record, output = await _execute_single_tool(tool, tc, round_num=1)

    assert record is not None
    assert record.tool is FailingTool
    assert "Intentional: crash" in output.content


async def test_execute_single_tool_receiver_uses_tool_ref_without_constructor_args() -> None:
    tool = _StatefulTool(api_key="secret-key-123")
    tc = make_tool_call("c1", "stateful_tool", '{"q": "test"}')
    database = _RecordingSpanDatabase()

    with set_execution_context(_make_context(database)):
        await _execute_single_tool(tool, tc, round_num=1)

    tool_call_span = _finished_spans(database, SpanKind.TOOL_CALL)[0]
    receiver = json.loads(tool_call_span.receiver_json)
    assert receiver["mode"] == "tool_ref"
    assert receiver["value"]["name"] == "_stateful_tool"
    assert "constructor_args" not in receiver
    assert "secret-key-123" not in tool_call_span.receiver_json


async def test_tool_loop_records_rounds_and_tool_calls(monkeypatch) -> None:
    call_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal call_count
        _ = (request, messages, round_index, runtime)
        call_count += 1
        if call_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),))
        return make_response(content="Found results")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context(database)):
        response, records, accumulated, llm_round_count = await _tool_loop(
            _request(), [CoreMessage(role=Role.USER, content="search for test")]
        )

    assert response.content == "Found results"
    assert len(records) == 1
    assert len(accumulated) == 3
    assert llm_round_count == 2
    assert [span.kind for span in _finished_spans(database, SpanKind.TOOL_CALL)] == [SpanKind.TOOL_CALL]


async def test_tool_loop_unknown_tool_has_no_record(monkeypatch) -> None:
    call_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal call_count
        _ = (request, messages, round_index, runtime)
        call_count += 1
        return (
            make_response(content="", tool_calls=(make_tool_call("c1", "missing_tool", "{}"),))
            if call_count == 1
            else make_response(content="done")
        )

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)
    response, records, accumulated, _round_count = await _tool_loop(
        _request(), [CoreMessage(role=Role.USER, content="hi")]
    )

    assert response.content == "done"
    assert records == []
    assert any(isinstance(message, ToolResultMessage) and "Unknown tool" in message.content for message in accumulated)
