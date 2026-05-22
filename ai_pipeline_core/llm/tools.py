"""Tool base class for LLM function calling.

Provides the public Tool class with import-time validation, schema generation,
and supporting types (ToolOutput, ToolCallRecord).

Tools are regular Python classes (not Pydantic models) with runtime state in __init__
and a framework-owned async execute() method wrapped around user-defined run().
"""

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar, get_type_hints

from pydantic import BaseModel, ConfigDict

from ai_pipeline_core._llm_core._strict_schema import reject_dynamic_key_objects

from ._tool_binding import ToolBinding

__all__ = ["Tool", "ToolCallRecord", "ToolOutput"]

logger = logging.getLogger(__name__)

_SNAKE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_RESERVED_FIELD_NAMES = frozenset({"strict", "additionalProperties"})


def _to_snake_case(name: str) -> str:
    """Convert PascalCase to snake_case, handling consecutive capitals.

    >>> _to_snake_case("HTTPClient")
    'http_client'
    >>> _to_snake_case("GetWeather")
    'get_weather'
    """
    return _SNAKE_RE.sub("_", name).lower()


class ToolOutput(BaseModel):
    """Container for serialized tool output sent to the LLM and caller metadata."""

    model_config = ConfigDict(frozen=True)

    content: str
    data: Any = None


class Tool:
    """Base class for LLM tools with import-time validation and a sealed lifecycle.

    Subclasses must define:
    - A non-empty docstring (becomes the LLM tool description)
    - An ``Input`` inner class (BaseModel with Field(description=...) on every field)
    - An ``Output`` inner class (BaseModel)
    - An ``async def run(self, input: Input) -> Output`` method

    Tool authors should not override ``execute()``. The framework owns:
    retries, timeout, structured error handling, and serialization.
    """

    Input: type[BaseModel]
    Output: type[BaseModel]
    _abstract_tool: ClassVar[bool] = False
    name: ClassVar[str]
    _tool_spec: ClassVar[Any]
    retries: ClassVar[int] = 0
    retry_delay_seconds: ClassVar[float] = 2.0
    timeout_seconds: ClassVar[int] = 120
    max_response_bytes: ClassVar[int | None] = None
    handled_exceptions: ClassVar[tuple[type[Exception], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.name = _to_snake_case(cls.__name__)

        if cls.__dict__.get("_abstract_tool", False) is True:
            return

        if "execute" in cls.__dict__:
            raise TypeError(
                f"Tool '{cls.name}' must not override execute(). "
                f"Implement 'async def run(self, input: Input) -> Output' instead. "
                f"The framework owns execute() and wraps run() with retry, timeout, and error handling."
            )

        _validate_tool_class(cls)

    async def run(self, input: Any) -> BaseModel:
        """Override this method with your tool logic. Return self.Output(...)."""
        raise NotImplementedError

    @classmethod
    def bind(cls, **kwargs: Any) -> ToolBinding:
        """Return a ``ToolBinding`` for this tool class with the given constructor args.

        Convenience over ``ToolBinding(MyTool, args={"key": value})``.
        """
        return ToolBinding(tool=cls, args=dict(kwargs))

    @staticmethod
    def _is_retryable(_error: Exception) -> bool:
        """Classify whether a handled exception should be retried. Default: False."""
        return False

    def handle_error(self, error: Exception) -> ToolOutput:
        """Format a handled exception into a caller-facing ToolOutput."""
        return ToolOutput(content=f"Error: Tool '{self.name}' failed: {error}")

    async def execute(self, input: BaseModel) -> ToolOutput:
        """Execute tool lifecycle (retry, timeout, errors, serialization)."""
        result: BaseModel | None = None
        for attempt in range(self.retries + 1):
            try:
                result = await asyncio.wait_for(self.run(input), timeout=self.timeout_seconds)
            except TimeoutError as timeout_exc:
                if attempt < self.retries:
                    logger.warning("Tool '%s' timed out (attempt %d/%d), retrying", self.name, attempt + 1, self.retries + 1, exc_info=timeout_exc)
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                logger.warning("Tool '%s' timed out after %d attempts", self.name, self.retries + 1, exc_info=timeout_exc)
                return ToolOutput(content=f"Error: Tool '{self.name}' timed out after {self.timeout_seconds}s ({self.retries + 1} attempts).")
            except self.handled_exceptions as error:
                if self._is_retryable(error) and attempt < self.retries:
                    logger.warning("Tool '%s' failed (attempt %d/%d), retrying", self.name, attempt + 1, self.retries + 1, exc_info=error)
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                try:
                    return self.handle_error(error)
                except Exception as handler_error:
                    logger.warning("Tool '%s' handle_error failed; re-raising original error", self.name, exc_info=handler_error)
                    raise error from handler_error
            else:
                break

        expected_output = type(self).Output
        if not isinstance(result, expected_output):
            raise TypeError(
                f"Tool '{self.name}'.run() must return {expected_output.__name__}, got {type(result).__name__}. Return self.Output(...) from run()."
            )

        data = result.model_dump(mode="json")
        content = json.dumps(data, indent=2)

        content_bytes = len(content.encode("utf-8"))
        if self.max_response_bytes is not None and content_bytes > self.max_response_bytes:
            return ToolOutput(
                content=json.dumps({
                    "error": "response_too_large",
                    "message": f"Tool '{self.name}' response exceeds {self.max_response_bytes} bytes. Use narrower filters or pagination.",
                    "actual_bytes": content_bytes,
                }),
                data=result,
            )

        return ToolOutput(content=content, data=result)


def _validate_tool_class(cls: type[Tool]) -> None:
    """Validate a concrete Tool subclass at definition time."""
    name = cls.name

    if not cls.__doc__ or not cls.__doc__.strip():
        raise TypeError(f"Tool '{name}' must define a non-empty docstring. The docstring becomes the LLM tool description.")

    _validate_tool_input_class(cls, name)
    _validate_tool_output_class(cls, name)
    _validate_tool_run_method(cls, name)
    _validate_lifecycle_classvars(cls, name)
    cls._tool_spec = True


def _validate_tool_input_class(cls: type[Tool], name: str) -> None:
    """Validate the Input model for a Tool subclass."""
    if "Input" in cls.__dict__:
        input_cls = cls.__dict__["Input"]
        if not isinstance(input_cls, type) or not issubclass(input_cls, BaseModel):
            raise TypeError(f"Tool '{name}'.Input must be a BaseModel subclass")
        for field_name, field_info in input_cls.model_fields.items():
            if field_info.description is None:
                raise TypeError(
                    f"Tool '{name}'.Input field '{field_name}' must use Field(description='...'). All Input fields require descriptions for the LLM."
                )
            if field_name in _RESERVED_FIELD_NAMES:
                raise TypeError(
                    f"Tool '{name}'.Input field '{field_name}' uses a reserved name that collides "
                    f"with JSON Schema or LiteLLM keywords. Some providers strip '{field_name}' from "
                    "schemas, causing required/properties mismatches. "
                    f"Rename the field (e.g., '{field_name}_value', '{field_name}_mode')."
                )
        reject_dynamic_key_objects(input_cls.model_json_schema(), context=f"Tool '{name}'.Input")
    elif not getattr(cls, "_tool_spec", None):
        raise TypeError(f"Tool '{name}' must define an 'Input' inner class (BaseModel). Example: class Input(BaseModel): query: str = Field(description='...')")


def _validate_tool_output_class(cls: type[Tool], name: str) -> None:
    """Validate the Output model for a Tool subclass."""
    if "Output" in cls.__dict__:
        output_cls = cls.__dict__["Output"]
        if not isinstance(output_cls, type) or not issubclass(output_cls, BaseModel):
            raise TypeError(f"Tool '{name}'.Output must be a BaseModel subclass")
        if issubclass(output_cls, ToolOutput):
            raise TypeError(f"Tool '{name}'.Output must extend BaseModel, not ToolOutput. The framework creates ToolOutput internally.")
    elif not getattr(cls, "_tool_spec", None):
        raise TypeError(f"Tool '{name}' must define an 'Output' inner class (BaseModel) or inherit one from a validated parent tool.")


def _validate_tool_run_method(cls: type[Tool], name: str) -> None:
    """Validate the run method for a Tool subclass."""
    if "run" in cls.__dict__:
        run_method = cls.__dict__["run"]
        if not inspect.iscoroutinefunction(run_method):
            raise TypeError(f"Tool '{name}'.run must be async (async def run)")
        signature = inspect.signature(run_method)
        params = list(signature.parameters.values())
        if len(params) != 2 or params[0].name != "self" or params[1].name != "input":
            raise TypeError(f"Tool '{name}'.run must have signature async def run(self, input: Input) -> Output.")
        module = inspect.getmodule(run_method)
        hints = get_type_hints(run_method, globalns=vars(module) if module is not None else {}, localns=dict(cls.__dict__))
        if hints.get("input") is not cls.Input:
            raise TypeError(f"Tool '{name}'.run input annotation must be the tool's Input class.")
        if hints.get("return") is not cls.Output:
            raise TypeError(f"Tool '{name}'.run return annotation must be the tool's Output class.")
    elif not getattr(cls, "_tool_spec", None):
        raise TypeError(f"Tool '{name}' must define an 'async def run(self, input: Input) -> Output' method or inherit one from a validated parent tool.")


def _validate_lifecycle_classvars(cls: type[Tool], name: str) -> None:
    """Validate lifecycle ClassVars and error handling methods on a concrete Tool."""
    if cls.retries < 0:
        raise TypeError(f"Tool '{name}' has invalid retries={cls.retries}. Use a value >= 0.")
    if cls.retry_delay_seconds <= 0:
        raise TypeError(f"Tool '{name}' has invalid retry_delay_seconds={cls.retry_delay_seconds}. Use a value > 0.")
    if cls.timeout_seconds <= 0:
        raise TypeError(f"Tool '{name}' has invalid timeout_seconds={cls.timeout_seconds}. Use a value > 0.")
    if cls.max_response_bytes is not None and cls.max_response_bytes <= 0:
        raise TypeError(f"Tool '{name}' has invalid max_response_bytes={cls.max_response_bytes}. Use None or a value > 0.")
    if not isinstance(cls.handled_exceptions, tuple):
        raise TypeError(f"Tool '{name}' handled_exceptions must be a tuple of Exception subclasses.")
    if any(not isinstance(exc_type, type) or not issubclass(exc_type, Exception) for exc_type in cls.handled_exceptions):
        raise TypeError(f"Tool '{name}' handled_exceptions must contain only Exception subclasses.")
    if inspect.iscoroutinefunction(cls._is_retryable):
        raise TypeError(f"Tool '{name}' _is_retryable must be sync; async definitions break execute() control flow.")
    if inspect.iscoroutinefunction(cls.handle_error):
        raise TypeError(f"Tool '{name}' handle_error must be sync; async definitions break execute() control flow.")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """Record of a single tool call execution within the tool loop."""

    tool: type[Tool]
    input: BaseModel
    output: ToolOutput
    round: int
