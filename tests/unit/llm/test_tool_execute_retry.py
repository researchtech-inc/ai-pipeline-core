"""Unit tests for tool retry behavior at the TOOL_CALL span boundary."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.types import RawToolCall
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.llm._engine import _execute_single_tool
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import set_execution_context
from tests.support.helpers import RecordingSpanDatabase, make_integration_context


class RetryThenSucceedTool(Tool):
    """Fail twice with a handled exception, then succeed."""

    retries: ClassVar[int] = 2
    retry_delay_seconds: ClassVar[float] = 0.01
    handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

    class Input(BaseModel):
        """Input for the retrying tool."""

        seed: int = Field(description="Seed value.")

    class Output(BaseModel):
        """Output from the retrying tool."""

        value: int = Field(description="Computed value.")

    def __init__(self) -> None:
        """Initialize per-instance attempt state."""
        self.attempts = 0

    def _is_retryable(self, error: Exception) -> bool:
        """All handled errors are retryable in this test tool."""
        _ = error
        return True

    async def run(self, input: Input) -> Output:
        """Return success on the third attempt."""
        self.attempts += 1
        if self.attempts < 3:
            raise ValueError("retryable failure")
        return self.Output(value=input.seed + 100)


class ExhaustedRetryTool(Tool):
    """Fail for every retry attempt with a handled retryable exception."""

    retries: ClassVar[int] = 2
    retry_delay_seconds: ClassVar[float] = 0.01
    handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

    class Input(BaseModel):
        """Input for the exhausted retry tool."""

        seed: int = Field(description="Seed value.")

    class Output(BaseModel):
        """Output from the exhausted retry tool."""

        value: int = Field(description="Computed value.")

    def __init__(self) -> None:
        """Initialize per-instance attempt state."""
        self.attempts = 0

    def _is_retryable(self, error: Exception) -> bool:
        """All handled errors are retryable in this test tool."""
        _ = error
        return True

    async def run(self, input: Input) -> Output:
        """Always fail with a retryable handled exception."""
        self.attempts += 1
        _ = input
        raise ValueError("retry budget exhausted")


class NonRetryableFailureTool(Tool):
    """Short-circuit after one handled exception marked non-retryable."""

    retries: ClassVar[int] = 2
    retry_delay_seconds: ClassVar[float] = 0.01
    handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

    class Input(BaseModel):
        """Input for the non-retryable tool."""

        seed: int = Field(description="Seed value.")

    class Output(BaseModel):
        """Output from the non-retryable tool."""

        value: int = Field(description="Computed value.")

    def __init__(self) -> None:
        """Initialize per-instance attempt state."""
        self.attempts = 0

    def _is_retryable(self, error: Exception) -> bool:
        """Handled errors are not retryable in this test tool."""
        _ = error
        return False

    async def run(self, input: Input) -> Output:
        """Fail once with a non-retryable handled exception."""
        self.attempts += 1
        _ = input
        raise ValueError("not retryable")


def _tool_call(tool: type[Tool] = RetryThenSucceedTool, arguments: str = '{"seed": 1}') -> RawToolCall:
    """Build a raw tool call for retry tests."""
    return RawToolCall(id="phase3-tool-call", function_name=tool.name, arguments=arguments)


async def test_tool_execute_retry_records_one_tool_call_span() -> None:
    """Internal Tool.execute retries must not create multiple TOOL_CALL spans."""
    database = RecordingSpanDatabase()
    tool = RetryThenSucceedTool()
    ctx = make_integration_context(database, run_id="phase3-tool-retry")

    with set_execution_context(ctx):
        record, output = await _execute_single_tool(tool, _tool_call(), round_num=1)

    spans = database.completed_spans_by_kind(SpanKind.TOOL_CALL)
    assert tool.attempts == 3
    assert record is not None
    assert record.tool is RetryThenSucceedTool
    assert isinstance(record.output.data, RetryThenSucceedTool.Output)
    assert record.output.data.value == 101
    assert output.data.value == 101
    assert len(spans) == 1
    assert "101" in spans[0].output_json
    assert "Error:" not in spans[0].output_json


async def test_tool_execute_exhausted_retries_returns_error_with_one_tool_call_span() -> None:
    """Exhausted handled retries return one error output under one TOOL_CALL span."""
    database = RecordingSpanDatabase()
    tool = ExhaustedRetryTool()
    ctx = make_integration_context(database, run_id="phase3-tool-retry-exhausted")

    with set_execution_context(ctx):
        record, output = await _execute_single_tool(tool, _tool_call(ExhaustedRetryTool), round_num=1)

    spans = database.completed_spans_by_kind(SpanKind.TOOL_CALL)
    assert tool.attempts == tool.retries + 1
    assert record is not None
    assert record.tool is ExhaustedRetryTool
    assert output.content.startswith("Error:")
    assert record.output.content == output.content
    assert len(spans) == 1
    assert "Error:" in spans[0].output_json


async def test_tool_execute_non_retryable_failure_short_circuits_with_one_tool_call_span() -> None:
    """A non-retryable handled failure stops after one attempt under one TOOL_CALL span."""
    database = RecordingSpanDatabase()
    tool = NonRetryableFailureTool()
    ctx = make_integration_context(database, run_id="phase3-tool-retry-nonretryable")

    with set_execution_context(ctx):
        record, output = await _execute_single_tool(tool, _tool_call(NonRetryableFailureTool), round_num=1)

    spans = database.completed_spans_by_kind(SpanKind.TOOL_CALL)
    assert tool.attempts == 1
    assert record is not None
    assert record.tool is NonRetryableFailureTool
    assert output.content.startswith("Error:")
    assert record.output.content == output.content
    assert len(spans) == 1
    assert "Error:" in spans[0].output_json
