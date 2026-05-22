"""Tests for llm/tools.py: Tool base class, schema generation, and validation."""

import asyncio
import inspect
import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm._tool_schema import generate_tool_schema
from ai_pipeline_core.llm.tools import Tool, ToolCallRecord, ToolOutput, _to_snake_case


# ── Valid Tool for reuse across tests ────────────────────────────────────────


class GetWeather(Tool):
    """Get the current weather for a location."""

    class Input(BaseModel):
        city: str = Field(description="City name")
        unit: str = Field(default="celsius", description="Temperature unit")

    class Output(BaseModel):
        weather: str

    async def run(self, input: Input) -> Output:
        return self.Output(weather=f"Weather in {input.city}: 20°")


class CustomOutputTool(Tool):
    """Tool with custom output."""

    class Input(BaseModel):
        query: str = Field(description="Search query")

    class Output(BaseModel):
        content: str
        source: str = "web"

    async def run(self, input: Input) -> Output:
        return self.Output(content="result", source="cache")


# ── Definition-time validation ───────────────────────────────────────────────


def test_tool_definition_valid() -> None:
    """Valid Tool subclass raises no errors."""
    assert GetWeather.__doc__
    assert issubclass(GetWeather.Input, BaseModel)


def test_tool_definition_missing_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class BadTool(Tool):
            class Input(BaseModel):
                x: int = Field(description="x value")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_tool_definition_missing_input() -> None:
    with pytest.raises(TypeError, match="must define an 'Input' inner class"):

        class BadTool(Tool):
            """Missing Input."""

            class Output(BaseModel):
                result: str

            async def run(self, input: BaseModel) -> Output:  # no Input class exists
                return self.Output(result="")


def test_tool_definition_input_not_basemodel() -> None:
    with pytest.raises(TypeError, match="Input must be a BaseModel subclass"):
        # Dynamic class creation avoids static type checker flagging the intentionally invalid override
        async def run(self: Tool, input: BaseModel) -> ToolOutput:  # pragma: no cover
            return ToolOutput(content="")

        type("BadTool", (Tool,), {"__doc__": "Bad input.", "Input": type("Input", (), {}), "Output": BaseModel, "run": run})


def test_tool_definition_input_field_missing_description() -> None:
    with pytest.raises(TypeError, match="must use Field\\(description="):

        class BadTool(Tool):
            """Missing field description."""

            class Input(BaseModel):
                query: str  # no Field(description=...)

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_tool_definition_non_async_execute() -> None:
    with pytest.raises(TypeError, match="run must be async"):

        class BadTool(Tool):
            """Sync run."""

            class Input(BaseModel):
                x: int = Field(description="x")

            class Output(BaseModel):
                result: str

            def run(self, input: Input) -> Output:  # type: ignore[override]
                return self.Output(result="")


def test_tool_definition_missing_run() -> None:
    with pytest.raises(TypeError, match="must define an 'async def run"):

        class BadTool(Tool):
            """No run method."""

            class Input(BaseModel):
                x: int = Field(description="x")

            class Output(BaseModel):
                result: str


def test_tool_definition_invalid_output_class() -> None:
    class BadOutput:
        content: str

    async def run(self: Tool, input: BaseModel) -> ToolOutput:  # pragma: no cover
        return ToolOutput(content="")

    with pytest.raises(TypeError, match="Output must be a BaseModel subclass"):
        # Dynamic class creation avoids static type checker flagging the intentionally invalid override
        type("BadTool", (Tool,), {"__doc__": "Bad output.", "Input": GetWeather.Input, "Output": BadOutput, "run": run})


def test_tool_definition_output_extending_tooloutput_rejected() -> None:
    class BadOutput(ToolOutput):
        content: str

    async def run(self: Tool, input: BaseModel) -> ToolOutput:  # pragma: no cover
        return ToolOutput(content="")

    with pytest.raises(TypeError, match="not ToolOutput"):
        type("BadTool", (Tool,), {"__doc__": "Bad output.", "Input": GetWeather.Input, "Output": BadOutput, "run": run})


def test_tool_with_custom_output() -> None:
    """Tool with custom Output extending BaseModel is valid."""
    assert issubclass(CustomOutputTool.Output, BaseModel)


# ── Schema generation ────────────────────────────────────────────────────────


def test_generate_tool_schema_basic() -> None:
    schema = generate_tool_schema(GetWeather())
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "get_weather"
    assert "Get the current weather" in func["description"]
    assert func["strict"] is True
    params = func["parameters"]
    assert params["type"] == "object"
    assert "city" in params["properties"]
    assert "unit" in params["properties"]
    assert params["additionalProperties"] is False


def test_generate_tool_schema_strict_mode_nested() -> None:
    """Strict mode recursively adds additionalProperties: false."""

    class NestedTool(Tool):
        """Tool with nested schema."""

        class Input(BaseModel):
            location: GetWeather.Input = Field(description="Location data")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

    schema = generate_tool_schema(NestedTool())
    params = schema["function"]["parameters"]
    # Nested model must produce $defs with strict mode applied
    assert "$defs" in params, "Nested model should produce $defs"
    for definition in params["$defs"].values():
        assert definition.get("additionalProperties") is False


def test_generate_tool_schema_all_fields_required() -> None:
    """Strict mode ensures all fields are in required list."""
    schema = generate_tool_schema(GetWeather())
    params = schema["function"]["parameters"]
    assert set(params["required"]) == {"city", "unit"}


# ── _to_snake_case ───────────────────────────────────────────────────────────


def test_snake_case_simple() -> None:
    assert _to_snake_case("GetWeather") == "get_weather"


def test_snake_case_consecutive_capitals() -> None:
    assert _to_snake_case("HTTPClient") == "http_client"


def test_snake_case_single_word() -> None:
    assert _to_snake_case("Search") == "search"


def test_snake_case_already_lower() -> None:
    assert _to_snake_case("search") == "search"


# ── ToolOutput ───────────────────────────────────────────────────────────────


def test_tool_output_frozen() -> None:
    from pydantic import ValidationError

    output = ToolOutput(content="hello")
    with pytest.raises(ValidationError):
        output.content = "world"  # type: ignore[misc]


# ── ToolCallRecord ───────────────────────────────────────────────────────────


def test_tool_call_record_frozen() -> None:
    tool_output = GetWeather.Output(weather="sunny")
    record = ToolCallRecord(
        tool=GetWeather,
        input=GetWeather.Input(city="Paris"),
        output=ToolOutput(content=tool_output.model_dump_json(), data=tool_output),
        round=1,
    )
    assert record.tool is GetWeather
    assert record.round == 1


# ── Phase 7a: Definition-time validation (new features) ─────────────────────


def test_abstract_tool_skips_validation() -> None:
    """Intermediate base with _abstract_tool=True and no Input/Output/run is valid."""

    class CryptoTool(Tool):
        """Base for crypto tools."""

        _abstract_tool: ClassVar[bool] = True

    assert CryptoTool.name == "crypto_tool"


def test_abstract_tool_not_inherited() -> None:
    """Concrete subclass of abstract base is still validated (flag not inherited)."""

    class BaseTool(Tool):
        """Abstract base."""

        _abstract_tool: ClassVar[bool] = True

    with pytest.raises(TypeError, match="must define an 'Input'"):

        class ConcreteTool(BaseTool):
            """Concrete but missing Input/Output/run."""


def test_abstract_tool_with_invalid_classvars_not_caught() -> None:
    """Abstract base can set retries=-1 without validation. Concrete subclass inheriting it raises TypeError."""

    class BadBase(Tool):
        """Abstract with invalid retries."""

        _abstract_tool: ClassVar[bool] = True
        retries: ClassVar[int] = -1

    with pytest.raises(TypeError, match="invalid retries"):

        class ConcreteTool(BadBase):
            """Concrete inheriting bad retries."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_sealed_execute_rejects_override() -> None:
    """Defining execute() on a Tool subclass raises TypeError at definition time."""
    with pytest.raises(TypeError, match="must not override execute"):

        class BadTool(Tool):
            """Overrides execute."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def execute(self, input: Input) -> ToolOutput:
                return ToolOutput(content="")


def test_output_required() -> None:
    """Missing Output on a concrete tool (no inheritance) raises TypeError."""
    with pytest.raises(TypeError, match="must define an 'Output'"):

        class NoOutputTool(Tool):
            """No Output."""

            class Input(BaseModel):
                x: str = Field(description="x")

            async def run(self, input: Input) -> BaseModel:
                return BaseModel()


def test_output_inherited_from_validated_parent() -> None:
    """Concrete subclass inherits Output+run from non-abstract validated parent."""

    class ParentTool(Tool):
        """Parent with Output and run."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="ok")

    class ChildTool(ParentTool):
        """Child inherits Output and run via _tool_spec."""

    assert ChildTool._tool_spec is True
    assert ChildTool.Output is ParentTool.Output


def test_output_not_inherited_from_abstract_parent() -> None:
    """Abstract parent defines Output but skips validation — concrete subclass cannot inherit it."""

    class AbstractBase(Tool):
        """Abstract with Output."""

        _abstract_tool: ClassVar[bool] = True

        class Output(BaseModel):
            result: str

    with pytest.raises(TypeError, match="must define an 'Input'"):

        class ConcreteTool(AbstractBase):
            """Missing Input, can't inherit Output from abstract."""


def test_run_inherited_from_validated_parent() -> None:
    """Concrete subclass inherits run() from non-abstract validated parent."""

    class ParentTool(Tool):
        """Parent."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="parent")

    class ChildTool(ParentTool):
        """Child inherits run."""

    assert ChildTool._tool_spec is True
    assert inspect.iscoroutinefunction(ChildTool.run)


def test_lifecycle_classvars_negative_retries() -> None:
    with pytest.raises(TypeError, match="invalid retries"):

        class BadTool(Tool):
            """Bad retries."""

            retries: ClassVar[int] = -1

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_lifecycle_classvars_zero_timeout() -> None:
    with pytest.raises(TypeError, match="invalid timeout_seconds"):

        class BadTool(Tool):
            """Bad timeout."""

            timeout_seconds: ClassVar[int] = 0

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_lifecycle_classvars_zero_retry_delay() -> None:
    with pytest.raises(TypeError, match="invalid retry_delay_seconds"):

        class BadTool(Tool):
            """Bad delay."""

            retry_delay_seconds: ClassVar[float] = 0.0

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_lifecycle_classvars_negative_max_response_bytes() -> None:
    with pytest.raises(TypeError, match="invalid max_response_bytes"):

        class BadTool(Tool):
            """Bad max bytes."""

            max_response_bytes: ClassVar[int | None] = -1

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_handled_exceptions_not_tuple() -> None:
    with pytest.raises(TypeError, match="handled_exceptions must be a tuple"):

        class BadTool(Tool):
            """Bad exceptions type."""

            handled_exceptions: ClassVar[Any] = ValueError  # type: ignore[assignment]

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_handled_exceptions_non_exception_entry() -> None:
    with pytest.raises(TypeError, match="handled_exceptions must contain only Exception"):

        class BadTool(Tool):
            """Non-Exception in tuple."""

            handled_exceptions: ClassVar[Any] = (str,)  # type: ignore[assignment]

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_handled_exceptions_valid_tuple() -> None:
    class GoodTool(Tool):
        """Valid exceptions."""

        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError, TimeoutError)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

    assert GoodTool.handled_exceptions == (ValueError, TimeoutError)


def test_async_handle_error_rejected() -> None:
    with pytest.raises(TypeError, match="handle_error must be sync"):

        class BadTool(Tool):
            """Async handle_error."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

            async def handle_error(self, error: Exception) -> ToolOutput:  # type: ignore[override]
                return ToolOutput(content="err")


def test_async_is_retryable_rejected() -> None:
    with pytest.raises(TypeError, match="_is_retryable must be sync"):

        class BadTool(Tool):
            """Async _is_retryable."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

            async def _is_retryable(self, error: Exception) -> bool:  # type: ignore[override]
                return False


def test_async_handle_error_inherited_from_abstract_base_rejected() -> None:
    """Abstract base defines async handle_error; concrete subclass inheriting it raises TypeError."""

    class AbstractBase(Tool):
        """Abstract with async handle_error."""

        _abstract_tool: ClassVar[bool] = True

        async def handle_error(self, error: Exception) -> ToolOutput:  # type: ignore[override]
            return ToolOutput(content="err")

    with pytest.raises(TypeError, match="handle_error must be sync"):

        class ConcreteTool(AbstractBase):
            """Inherits async handle_error."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_async_is_retryable_inherited_from_abstract_base_rejected() -> None:
    """Abstract base defines async _is_retryable; concrete subclass inheriting it raises TypeError."""

    class AbstractBase(Tool):
        """Abstract with async _is_retryable."""

        _abstract_tool: ClassVar[bool] = True

        async def _is_retryable(self, error: Exception) -> bool:  # type: ignore[override]
            return False

    with pytest.raises(TypeError, match="_is_retryable must be sync"):

        class ConcreteTool(AbstractBase):
            """Inherits async _is_retryable."""

            class Input(BaseModel):
                x: str = Field(description="x")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")


def test_sync_handle_error_accepted() -> None:
    class GoodTool(Tool):
        """Sync handle_error override."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

        def handle_error(self, error: Exception) -> ToolOutput:
            return ToolOutput(content=f"Custom: {error}")

    assert not inspect.iscoroutinefunction(GoodTool.handle_error)


def test_sync_is_retryable_accepted() -> None:
    class GoodTool(Tool):
        """Sync _is_retryable override."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

        def _is_retryable(self, error: Exception) -> bool:
            return True

    assert not inspect.iscoroutinefunction(GoodTool._is_retryable)


def test_name_classvar_computed() -> None:
    assert GetWeather.name == "get_weather"
    assert CustomOutputTool.name == "custom_output_tool"


def test_name_classvar_consecutive_capitals() -> None:
    class HTTPClient(Tool):
        """HTTP client tool."""

        class Input(BaseModel):
            url: str = Field(description="URL")

        class Output(BaseModel):
            status: int

        async def run(self, input: Input) -> Output:
            return self.Output(status=200)

    assert HTTPClient.name == "http_client"


def test_tool_spec_set_after_validation() -> None:
    assert GetWeather._tool_spec is True


def test_concrete_tool_inherits_lifecycle_classvars() -> None:
    class BaseTool(Tool):
        """Base with lifecycle config."""

        _abstract_tool: ClassVar[bool] = True
        retries: ClassVar[int] = 3
        timeout_seconds: ClassVar[int] = 60

    class ConcreteTool(BaseTool):
        """Concrete inheriting lifecycle."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

    assert ConcreteTool.retries == 3
    assert ConcreteTool.timeout_seconds == 60


# ── Phase 7b: Sealed execute() lifecycle ─────────────────────────────────────


class _SimpleTool(Tool):
    """Tool for execute lifecycle tests."""

    class Input(BaseModel):
        value: str = Field(description="Input value")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        return self.Output(result=f"got:{input.value}")


async def test_sealed_execute_serializes_output_to_json() -> None:
    tool = _SimpleTool()
    output = await tool.execute(_SimpleTool.Input(value="hello"))
    data = json.loads(output.content)
    assert data == {"result": "got:hello"}
    assert "\n" in output.content  # indented JSON


async def test_sealed_execute_data_field_contains_output_instance() -> None:
    tool = _SimpleTool()
    output = await tool.execute(_SimpleTool.Input(value="x"))
    assert isinstance(output.data, _SimpleTool.Output)
    assert output.data.result == "got:x"


async def test_sealed_execute_validates_return_type() -> None:
    class WrongOutput(BaseModel):
        wrong: str

    class WrongReturnTool(Tool):
        """Returns wrong type."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return WrongOutput(wrong="bad")  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must return Output"):
        await WrongReturnTool().execute(WrongReturnTool.Input(x="test"))


async def test_sealed_execute_retries_on_retryable_error() -> None:
    call_count = 0

    class RetryTool(Tool):
        """Retries on ValueError."""

        retries: ClassVar[int] = 2
        retry_delay_seconds: ClassVar[float] = 0.01
        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        def _is_retryable(self, error: Exception) -> bool:
            return True

        async def run(self, input: Input) -> Output:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("retry me")
            return self.Output(result="ok")

    output = await RetryTool().execute(RetryTool.Input(x="test"))
    assert call_count == 3
    assert json.loads(output.content)["result"] == "ok"


async def test_sealed_execute_no_retry_when_not_retryable() -> None:
    call_count = 0

    class NoRetryTool(Tool):
        """Does not retry."""

        retries: ClassVar[int] = 3
        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            nonlocal call_count
            call_count += 1
            raise ValueError("no retry")

    output = await NoRetryTool().execute(NoRetryTool.Input(x="test"))
    assert call_count == 1
    assert "failed" in output.content


async def test_sealed_execute_retries_exhausted() -> None:
    call_count = 0

    class ExhaustTool(Tool):
        """All retries fail."""

        retries: ClassVar[int] = 2
        retry_delay_seconds: ClassVar[float] = 0.01
        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        def _is_retryable(self, error: Exception) -> bool:
            return True

        async def run(self, input: Input) -> Output:
            nonlocal call_count
            call_count += 1
            raise ValueError("always fail")

    output = await ExhaustTool().execute(ExhaustTool.Input(x="test"))
    assert call_count == 3  # 1 original + 2 retries
    assert "failed" in output.content


async def test_sealed_execute_timeout_per_attempt() -> None:
    class TimeoutTool(Tool):
        """Times out on every attempt."""

        retries: ClassVar[int] = 1
        retry_delay_seconds: ClassVar[float] = 0.01
        timeout_seconds: ClassVar[int] = 1

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            await asyncio.Future()
            return self.Output(result="never")

    output = await TimeoutTool().execute(TimeoutTool.Input(x="test"))
    assert "timed out" in output.content
    assert "2 attempts" in output.content


async def test_sealed_execute_timeout_zero_retries() -> None:
    class TimeoutNoRetry(Tool):
        """Timeout with no retries."""

        timeout_seconds: ClassVar[int] = 1

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            await asyncio.Future()
            return self.Output(result="never")

    output = await TimeoutNoRetry().execute(TimeoutNoRetry.Input(x="test"))
    assert "timed out" in output.content
    assert "1 attempts" in output.content


async def test_sealed_execute_handle_error_bug_reraises_original() -> None:
    class BuggyHandlerTool(Tool):
        """handle_error itself crashes."""

        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            raise ValueError("original")

        def handle_error(self, error: Exception) -> ToolOutput:
            raise RuntimeError("handle_error bug")

    with pytest.raises(ValueError, match="original"):
        await BuggyHandlerTool().execute(BuggyHandlerTool.Input(x="test"))


async def test_sealed_execute_unhandled_exception_propagates() -> None:
    class CrashTool(Tool):
        """Raises unhandled exception."""

        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            raise RuntimeError("unhandled")

    with pytest.raises(RuntimeError, match="unhandled"):
        await CrashTool().execute(CrashTool.Input(x="test"))


async def test_sealed_execute_empty_handled_exceptions() -> None:
    class EmptyHandled(Tool):
        """Empty handled_exceptions tuple."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            raise ValueError("not caught")

    with pytest.raises(ValueError, match="not caught"):
        await EmptyHandled().execute(EmptyHandled.Input(x="test"))


async def test_sealed_execute_cancelled_error_propagates() -> None:
    class CancelTool(Tool):
        """Raises CancelledError."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await CancelTool().execute(CancelTool.Input(x="test"))


async def test_sealed_execute_max_response_bytes_under_limit() -> None:
    class SmallTool(Tool):
        """Output under limit."""

        max_response_bytes: ClassVar[int | None] = 10000

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="small")

    output = await SmallTool().execute(SmallTool.Input(x="test"))
    assert json.loads(output.content)["result"] == "small"
    assert output.data is not None


async def test_sealed_execute_max_response_bytes_over_limit() -> None:
    class BigTool(Tool):
        """Output over limit."""

        max_response_bytes: ClassVar[int | None] = 10

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="x" * 100)

    output = await BigTool().execute(BigTool.Input(x="test"))
    error = json.loads(output.content)
    assert error["error"] == "response_too_large"


async def test_sealed_execute_max_response_bytes_over_limit_preserves_data() -> None:
    class BigTool(Tool):
        """Data preserved even when over limit."""

        max_response_bytes: ClassVar[int | None] = 10

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="big")

    output = await BigTool().execute(BigTool.Input(x="test"))
    assert output.data is not None
    assert output.data.result == "big"


async def test_sealed_execute_max_response_bytes_none_no_limit() -> None:
    output = await _SimpleTool().execute(_SimpleTool.Input(value="test"))
    assert _SimpleTool.max_response_bytes is None
    assert json.loads(output.content)["result"] == "got:test"


async def test_sealed_execute_max_response_bytes_boundary() -> None:
    class BoundaryTool(Tool):
        """Test exact boundary."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="ok")

    tool = BoundaryTool()
    output = await tool.execute(BoundaryTool.Input(x="test"))
    exact_size = len(output.content.encode("utf-8"))

    # At exact size: should pass
    BoundaryTool.max_response_bytes = exact_size
    output = await tool.execute(BoundaryTool.Input(x="test"))
    assert "response_too_large" not in output.content

    # One byte under: should fail
    BoundaryTool.max_response_bytes = exact_size - 1
    output = await tool.execute(BoundaryTool.Input(x="test"))
    assert json.loads(output.content)["error"] == "response_too_large"


async def test_sealed_execute_exclude_field() -> None:
    class ExcludeTool(Tool):
        """Field(exclude=True) hidden from content but present in data."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str
            secret: int = Field(exclude=True, default=42)

        async def run(self, input: Input) -> Output:
            return self.Output(result="visible", secret=99)

    output = await ExcludeTool().execute(ExcludeTool.Input(x="test"))
    content = json.loads(output.content)
    assert "secret" not in content
    assert content["result"] == "visible"
    assert output.data.secret == 99


async def test_sealed_execute_custom_handle_error() -> None:
    class CustomErrorTool(Tool):
        """Custom handle_error."""

        handled_exceptions: ClassVar[tuple[type[Exception], ...]] = (ValueError,)

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            raise ValueError("bad input")

        def handle_error(self, error: Exception) -> ToolOutput:
            return ToolOutput(content=f"Custom error: {error}")

    output = await CustomErrorTool().execute(CustomErrorTool.Input(x="test"))
    assert output.content == "Custom error: bad input"


async def test_sealed_execute_model_dump_mode_json() -> None:
    """Output with non-primitive fields: model_dump(mode='json') produces serializable values."""
    from datetime import datetime
    from uuid import UUID

    class RichTool(Tool):
        """Tool with rich output types."""

        class Input(BaseModel):
            x: str = Field(description="x")

        class Output(BaseModel):
            ts: datetime
            uid: UUID

        async def run(self, input: Input) -> Output:
            return self.Output(ts=datetime(2026, 1, 1), uid=UUID("12345678-1234-5678-1234-567812345678"))

    output = await RichTool().execute(RichTool.Input(x="test"))
    data = json.loads(output.content)
    assert data["ts"] == "2026-01-01T00:00:00"
    assert data["uid"] == "12345678-1234-5678-1234-567812345678"
