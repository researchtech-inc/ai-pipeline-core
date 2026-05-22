"""Private LLM execution engine used by Conversation."""

import asyncio
import json
import logging
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from ai_pipeline_core._llm_core.client import generate
from ai_pipeline_core._llm_core.exceptions import TerminalError
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import (
    CacheSpec,
    DebugSpec,
    GenerationSpec,
    ListOf,
    LLMRequest,
    ResponseSpec,
    RetrySpec,
    RoutingSpec,
    ToolSpec,
    ValidationSpec,
)
from ai_pipeline_core._llm_core.types import AIModel, CoreMessage, RawToolCall, Role, TokenUsage
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.llm._conversation_messages import (
    AnyMessage,
    ToolResultMessage,
    _core_messages_to_db_span_input,
    _response_format_path,
    _serialize_response_tool_calls,
)
from ai_pipeline_core.llm.tools import Tool, ToolCallRecord, ToolOutput
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_sinks
from ai_pipeline_core.pipeline._span_sink import SpanContext
from ai_pipeline_core.pipeline._track_span import track_span

from ._substitutor import URLSubstitutor

__all__ = ["EngineResult", "InteractionRequest", "ToolRuntime", "_replay_llm_round", "execute_interaction"]

logger = logging.getLogger(__name__)
# Internal safety cap for repeated invalid tool names; intentionally not public configuration.
MAX_CONSECUTIVE_UNKNOWN_TOOL_ROUNDS = 3

type _ToolDispatchResult = tuple[RawToolCall, ToolCallRecord | None, ToolOutput]

_FORCED_FINAL_MAX_ROUNDS_MSG = (
    "You have reached the maximum number of tool-call rounds. No more tools are available in this conversation. "
    "Do not call any tools or functions. Use only the information already present in the conversation and the tool results "
    "already returned to produce the final answer now. If a structured response format was requested earlier, follow it exactly. "
    "If the available information is insufficient, explain the limitation instead of requesting another tool."
)
_FORCED_FINAL_UNKNOWN_TOOLS_MSG = (
    "Tool calling has been stopped because your recent requests targeted tools that do not exist. "
    "No more tools are available in this conversation. Do not call any tools or functions. Use only the information already present "
    "in the conversation and the tool results already returned to produce the final answer now. "
    "If a structured response format was requested earlier, follow it exactly. If the available information is insufficient, "
    "explain the limitation instead of requesting another tool."
)


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Engine-layer tool availability.

    ``max_calls_by_name`` caps the total executions of each tool class
    across one execution (tool loop + repair attempts inside one
    ``PromptContract.execute()`` or ``Conversation.send()``). The engine
    decrements per-tool budgets in ``_execute_tools`` and returns a
    budget-exhausted ``ToolOutput`` to the model for any over-budget call
    without invoking the tool. Tools missing from the mapping are
    unmetered (legacy ``Conversation.send(tools=[...])`` callers).
    """

    schemas: tuple[dict[str, Any], ...]
    instances: Mapping[str, Tool]
    choice: str | None = None
    max_rounds: int = 10
    max_calls_by_name: Mapping[str, int] = field(default_factory=dict)

    def to_spec(self, *, round_index: int) -> ToolSpec:
        """Project runtime state to transport schemas for one round."""
        return ToolSpec(schemas=self.schemas, choice=self.choice if round_index == 0 else None)


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Engine output consumed by Conversation."""

    response: ModelResponse[Any]
    tool_call_records: tuple[ToolCallRecord, ...]
    accumulated_messages: tuple[AnyMessage, ...]
    final_aipl_call_id: str
    final_deployment_id: str | None
    llm_round_count: int
    repair_attempt_count: int = 0
    """Number of validation repair re-entries (0 when the first attempt passed)."""


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """Engine input created by Conversation or internal framework callers."""

    messages: tuple[CoreMessage, ...]
    model: AIModel
    context_count: int = 0
    response_format: type[BaseModel] | ListOf | None = None
    tools: ToolRuntime | None = None
    substitutor: URLSubstitutor | None = None
    purpose: str | None = None
    generation_overrides: GenerationSpec | None = None
    retry_overrides: RetrySpec | None = None
    cache_overrides: CacheSpec | None = None
    routing_overrides: RoutingSpec | None = None
    debug_overrides: DebugSpec | None = None
    validation: ValidationSpec | None = None


async def execute_interaction(request: InteractionRequest) -> EngineResult:  # noqa: PLR0914 — repair loop unavoidably tracks attempt state, accumulators, and per-tool budgets.
    """Execute one interaction end to end (with optional repair loop)."""
    validation = request.validation
    max_attempts = validation.max_attempts if validation is not None else 1

    accumulated: list[AnyMessage] = []
    records: list[ToolCallRecord] = []
    llm_round_count = 0
    messages: list[CoreMessage] = list(request.messages)
    current_request = request
    last_response: ModelResponse[Any] | None = None
    last_failures: tuple[Any, ...] = ()
    final_attempt_index = 0

    # Per-tool call budget — counts down across all tool-loop rounds AND
    # across repair attempts within this one execution. Tools missing from
    # the mapping are unmetered (legacy Conversation.send callers).
    remaining_calls: dict[str, int] = dict(request.tools.max_calls_by_name) if request.tools is not None else {}

    for attempt_index in range(max_attempts):
        final_attempt_index = attempt_index
        response, attempt_records, attempt_accumulated, attempt_round_count = await _tool_loop(current_request, messages, remaining_calls)
        records.extend(attempt_records)
        accumulated.extend(attempt_accumulated)
        llm_round_count += attempt_round_count
        last_response = response

        last_failures = _run_validation(validation, response)
        if not last_failures:
            break

        if attempt_index + 1 >= max_attempts:
            # Exhausted — exit loop, raise below.
            break

        messages = _extend_for_repair(messages, response, last_failures)
        current_request = _request_with_repair_routing(current_request, response.transport.aipl.deployment_id)

    if validation is not None and last_failures:
        formatted = ", ".join(_failure_summary(failure) for failure in last_failures)
        raise TerminalError(f"PromptContract validation exhausted after {max_attempts} attempt(s); failures: {formatted}.")

    assert last_response is not None
    # ``repair_attempt_count`` is the number of validation-driven re-entries:
    # 0 when the first attempt passed, 1 when one repair succeeded, etc.
    return EngineResult(
        response=last_response,
        tool_call_records=tuple(records),
        accumulated_messages=tuple(accumulated),
        final_aipl_call_id=last_response.transport.aipl.call_id or "",
        final_deployment_id=last_response.transport.aipl.deployment_id,
        llm_round_count=llm_round_count,
        repair_attempt_count=final_attempt_index,
    )


def _run_validation(validation: ValidationSpec | None, response: ModelResponse[Any]) -> tuple[Any, ...]:
    if validation is None:
        return ()
    parsed = response.parsed
    if not isinstance(parsed, BaseModel):
        # Validation only applies to structured (BaseModel) outputs. Non-structured
        # responses pass through; the contract layer is expected to enforce the
        # response_format separately.
        return ()
    failures = validation.validate(parsed)
    if not failures:
        return ()
    return tuple(failures)


def _extend_for_repair(
    messages: list[CoreMessage],
    response: ModelResponse[Any],
    failures: Sequence[Any],
) -> list[CoreMessage]:
    """Append the failing assistant response and a USER repair message to the history."""
    extended: list[CoreMessage] = list(messages)
    extended.append(
        CoreMessage(
            role=Role.ASSISTANT,
            content=response.content or "",
            tool_calls=response.tool_calls or None,
            provider_specific_fields=response.provider_specific_fields,
            thinking_blocks=response.thinking_blocks,
        )
    )
    extended.append(CoreMessage(role=Role.USER, content=_render_repair(failures)))
    return extended


def _request_with_repair_routing(request: InteractionRequest, deployment_id: str | None) -> InteractionRequest:
    """Thread the prior attempt's deployment_id into routing for soft affinity."""
    if not deployment_id:
        return request
    base_routing = request.routing_overrides or RoutingSpec()
    new_routing = replace(base_routing, preferred_deployment_id=deployment_id)
    return replace(request, routing_overrides=new_routing)


def _render_repair(failures: Sequence[Any]) -> str:
    """Render validation failures into a USER repair message body."""
    lines = ["Your previous response did not satisfy the contract validation."]
    for failure in failures:
        field_path = getattr(failure, "field", None)
        message = getattr(failure, "message", None) or str(failure)
        if field_path:
            lines.append(f"- [{field_path}] {message}")
        else:
            lines.append(f"- {message}")
    lines.append(
        "Re-emit the structured response that conforms exactly to the declared output schema and addresses every issue above. "
        "Do not include prose, markdown, or commentary."
    )
    return "\n".join(lines)


def _failure_summary(failure: Any) -> str:
    field_path = getattr(failure, "field", None)
    message = getattr(failure, "message", None) or str(failure)
    if field_path:
        return f"[{field_path}] {message}"
    return str(message)


async def _tool_loop(
    request: InteractionRequest,
    messages: list[CoreMessage],
    remaining_calls: dict[str, int] | None = None,
) -> tuple[ModelResponse[Any], list[ToolCallRecord], list[AnyMessage], int]:
    runtime = request.tools
    max_rounds = runtime.max_rounds if runtime is not None else 1
    records: list[ToolCallRecord] = []
    accumulated: list[Any] = []
    llm_round_count = 0
    consecutive_unknown_rounds = 0

    for round_index in range(max_rounds):
        response = await _single_call(request, messages, round_index, runtime)
        llm_round_count += 1
        accumulated.append(response)
        if runtime is None or not response.has_tool_calls:
            return response, records, accumulated, llm_round_count

        messages.append(
            CoreMessage(
                role=Role.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls,
                provider_specific_fields=response.provider_specific_fields,
                thinking_blocks=response.thinking_blocks,
            )
        )
        tool_outputs = await _execute_tools(response.tool_calls, runtime.instances, round_index + 1, remaining_calls)
        for tool_call, record, output in tool_outputs:
            if record is not None:
                records.append(record)
            tool_content_for_llm = request.substitutor.substitute(output.content) if request.substitutor is not None else output.content
            messages.append(CoreMessage(role=Role.TOOL, content=tool_content_for_llm, tool_call_id=tool_call.id, name=tool_call.function_name))
            accumulated.append(ToolResultMessage(tool_call_id=tool_call.id, function_name=tool_call.function_name, content=output.content))

        if all(tool_call.function_name not in runtime.instances for tool_call in response.tool_calls):
            consecutive_unknown_rounds += 1
            if consecutive_unknown_rounds >= MAX_CONSECUTIVE_UNKNOWN_TOOL_ROUNDS:
                break
        else:
            consecutive_unknown_rounds = 0

    final_message = _FORCED_FINAL_UNKNOWN_TOOLS_MSG if consecutive_unknown_rounds >= MAX_CONSECUTIVE_UNKNOWN_TOOL_ROUNDS else _FORCED_FINAL_MAX_ROUNDS_MSG
    messages.append(CoreMessage(role=Role.USER, content=final_message))
    try:
        response = await _single_call(request, messages, max_rounds, None)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):  # fmt: skip
        raise
    except Exception as exc:
        logger.warning(
            "Tool loop exhausted after %s tool call(s), and forced final synthesis failed for model=%s: %s",
            len(records),
            request.model.name,
            exc,
            exc_info=exc,
        )
        response = _synthetic_forced_final_response(model_name=request.model.name)
    llm_round_count += 1
    accumulated.append(response)
    return response, records, accumulated, llm_round_count


def _synthetic_forced_final_response(*, model_name: str) -> ModelResponse[Any]:
    content = "[Final synthesis failed after max tool rounds. Tool results are preserved in tool_call_records.]"
    return ModelResponse[Any](
        content=content,
        parsed=content,
        usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        model=model_name,
        finish_reason="stop",
    )


async def _single_call(
    request: InteractionRequest,
    messages: list[CoreMessage],
    round_index: int,
    runtime: ToolRuntime | None,
) -> ModelResponse[Any]:
    tool_spec = runtime.to_spec(round_index=round_index) if runtime is not None else ToolSpec()
    llm_request = _build_llm_request(request, tuple(messages), tool_spec)
    execution_ctx = get_execution_context()
    async with track_span(
        SpanKind.LLM_ROUND,
        f"round-{round_index + 1}",
        "function:ai_pipeline_core.llm._engine:_replay_llm_round",
        sinks=get_sinks(),
        encode_input={"llm_request": llm_request, "round_index": round_index + 1},
        db=execution_ctx.database if execution_ctx is not None else None,
    ) as span_ctx:
        response = await generate(llm_request)
        _record_round_meta(span_ctx, response, llm_request)
        span_ctx.set_output_preview([response.reasoning_content, response.content] if response.reasoning_content else response.content)
        span_ctx._set_output_value(response)
        return response


def _build_llm_request(
    request: InteractionRequest,
    messages: tuple[CoreMessage, ...],
    tool_spec: ToolSpec,
) -> LLMRequest:
    model = request.model
    generation_override = request.generation_overrides
    retry_override = request.retry_overrides
    cache_override = request.cache_overrides
    routing_override = request.routing_overrides
    debug_override = request.debug_overrides
    return LLMRequest(
        model=model,
        messages=messages,
        generation=GenerationSpec(
            temperature=_pick(generation_override.temperature if generation_override else None, model.temperature),
            reasoning_effort=_pick(generation_override.reasoning_effort if generation_override else None, model.reasoning_effort),
            verbosity=_pick(generation_override.verbosity if generation_override else None, model.verbosity),
            max_completion_tokens=_pick(generation_override.max_completion_tokens if generation_override else None, model.max_completion_tokens),
            stop=generation_override.stop if generation_override else None,
        ),
        cache=CacheSpec(
            context_count=cache_override.context_count if cache_override and cache_override.context_count else request.context_count,
            ttl=cache_override.ttl if cache_override else model.cache_ttl,
            key_override=cache_override.key_override if cache_override else None,
            bypass_response_cache=cache_override.bypass_response_cache if cache_override else False,
        ),
        routing=RoutingSpec(
            workload_id=routing_override.workload_id if routing_override else None,
            force_deployment_id=routing_override.force_deployment_id if routing_override else None,
            preferred_deployment_id=routing_override.preferred_deployment_id if routing_override else None,
            skip_cost_optimized=_pick(routing_override.skip_cost_optimized if routing_override else None, model.skip_cost_optimized),
            skip_ids=routing_override.skip_ids if routing_override else frozenset(),
        ),
        response=ResponseSpec(format=request.response_format),
        tools=tool_spec,
        retry=RetrySpec(
            retries=retry_override.retries if retry_override else None,
            retry_delay_seconds=retry_override.retry_delay_seconds if retry_override else None,
            timeout_s=_pick(retry_override.timeout_s if retry_override else None, model.timeout_s),
            min_output_tps=_pick(retry_override.min_output_tps if retry_override else None, model.min_output_tps),
        ),
        debug=DebugSpec(
            capture_trace=debug_override.capture_trace if debug_override else False,
            disable_watchdog=debug_override.disable_watchdog if debug_override else False,
            warmup_strategy_override=debug_override.warmup_strategy_override if debug_override else None,
        ),
        purpose=request.purpose,
    )


async def _execute_tools(
    tool_calls: tuple[RawToolCall, ...],
    tools: Mapping[str, Tool],
    round_index: int,
    remaining_calls: dict[str, int] | None = None,
) -> list[tuple[RawToolCall, ToolCallRecord | None, ToolOutput]]:
    """Execute one round of tool calls in parallel, enforcing per-tool budgets.

    ``remaining_calls`` (when present) maps tool snake_case name to remaining
    call budget. Decremented for each call that is dispatched; calls that
    would push the budget below zero short-circuit with a budget-exhausted
    synthetic ToolOutput before the tool runs. Decisions are made sequentially
    here so the asyncio.gather below never schedules more than the budget
    allows (no decrement-after-gather race).
    """

    def _budget_exhausted(tool_call: RawToolCall) -> tuple[RawToolCall, ToolCallRecord | None, ToolOutput]:
        return (
            tool_call,
            None,
            ToolOutput(
                content=(
                    f"Error: Tool '{tool_call.function_name}' has exhausted its max_calls budget for this execution. "
                    "Do not call it again; use the results already returned or explain the limitation in the final answer."
                )
            ),
        )

    def _unknown(tool_call: RawToolCall) -> tuple[RawToolCall, ToolCallRecord | None, ToolOutput]:
        available = ", ".join(sorted(tools.keys()))
        return tool_call, None, ToolOutput(content=f"Error: Unknown tool '{tool_call.function_name}'. Available tools: {available}")

    async def _run(tool: Tool, tool_call: RawToolCall) -> tuple[RawToolCall, ToolCallRecord | None, ToolOutput]:
        record, output = await _execute_single_tool(tool, tool_call, round_index)
        return tool_call, record, output

    # Decide synchronously which calls execute vs. short-circuit, then gather only the runners.
    # Indexed by position in tool_calls so we can re-merge results in order.
    short_circuits: dict[int, _ToolDispatchResult] = {}
    runners: list[tuple[int, Coroutine[Any, Any, _ToolDispatchResult]]] = []
    for index, tool_call in enumerate(tool_calls):
        tool = tools.get(tool_call.function_name)
        if tool is None:
            short_circuits[index] = _unknown(tool_call)
            continue
        if remaining_calls is not None and tool_call.function_name in remaining_calls:
            if remaining_calls[tool_call.function_name] <= 0:
                short_circuits[index] = _budget_exhausted(tool_call)
                continue
            remaining_calls[tool_call.function_name] -= 1
        runners.append((index, _run(tool, tool_call)))

    runner_results = await asyncio.gather(*(coro for _, coro in runners), return_exceptions=True)
    # Build the merged list in original order.
    by_index: dict[int, _ToolDispatchResult | BaseException] = dict(short_circuits)
    for (index, _), result in zip(runners, runner_results, strict=True):
        by_index[index] = result
    results = [by_index[i] for i in range(len(tool_calls))]
    merged: list[tuple[RawToolCall, ToolCallRecord | None, ToolOutput]] = []
    for index, result in enumerate(results):
        if isinstance(result, BaseException):
            if isinstance(result, (TypeError, AssertionError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise result
            tool_call = tool_calls[index]
            logger.warning("Unexpected error executing tool '%s': %s", tool_call.function_name, result, exc_info=result)
            merged.append((tool_call, None, ToolOutput(content=f"Error: {result}")))
            continue
        merged.append(result)
    return merged


def _record_round_meta(span_ctx: SpanContext, response: ModelResponse[Any], llm_request: LLMRequest) -> None:
    transport = response.transport
    span_ctx.set_meta(
        model=response.model,
        finish_reason=response.finish_reason,
        response_id=response.response_id,
        tool_schemas=list(llm_request.tools.schemas),
        request_messages=_core_messages_to_db_span_input(list(llm_request.messages)),
        response_content=response.content,
        citations=[citation.model_dump(mode="json") for citation in response.citations],
        response_tool_calls=_serialize_response_tool_calls(response.tool_calls),
        response_format_path=_response_format_path(llm_request.response.format),
        tool_call_count=len(response.tool_calls),
        tool_choice=llm_request.tools.choice,
        aipl=asdict(transport.aipl),
        litellm=asdict(transport.litellm),
        model_chain=asdict(transport.model_chain),
        prompt_cache_key=transport.prompt_cache_key,
        raw_response_headers=dict(transport.raw_response_headers),
    )
    span_ctx.set_metrics(
        tokens_input=response.usage.prompt_tokens,
        tokens_output=response.usage.completion_tokens,
        tokens_cache_read=response.usage.cached_tokens,
        tokens_reasoning=response.usage.reasoning_tokens,
        cost_usd=response.cost or 0.0,
        first_token_ms=int((transport.timing.first_token_s or 0) * 1000),
    )


async def _replay_llm_round(
    *,
    llm_request: LLMRequest,
) -> ModelResponse[Any]:
    """Replay target for LLM_ROUND spans recorded by the engine."""
    return await generate(llm_request)


def _pick[T](override_value: T | None, model_value: T | None) -> T | None:
    return override_value if override_value is not None else model_value


async def _execute_single_tool(
    tool: Tool,
    tool_call: RawToolCall,
    round_num: int,
) -> tuple[ToolCallRecord | None, ToolOutput]:
    """Execute one tool call and record a TOOL_CALL span."""
    tool_cls = type(tool)
    snake_name = tool_cls.name
    execution_ctx = get_execution_context()
    tool_target = f"tool_call:{tool_cls.__module__}:{tool_cls.__qualname__}"
    receiver_payload = {
        "mode": "tool_ref",
        "value": {"name": tool_cls.name, "class_path": f"{tool_cls.__module__}:{tool_cls.__qualname__}"},
    }

    async with track_span(
        SpanKind.TOOL_CALL,
        snake_name,
        tool_target,
        sinks=get_sinks(),
        encode_receiver=receiver_payload,
        encode_input={"input": _parse_tool_arguments(tool_call.arguments), "tool_call_id": tool_call.id, "round_index": round_num},
        db=execution_ctx.database if execution_ctx is not None else None,
        input_preview={"tool_name": snake_name, "arguments": _parse_tool_arguments(tool_call.arguments)},
    ) as span_ctx:
        try:
            parsed_input = tool_cls.Input.model_validate_json(tool_call.arguments)
        except (ValidationError, json.JSONDecodeError) as exc:  # fmt: skip
            output = ToolOutput(content=f"Error: Invalid arguments for tool '{snake_name}': {exc}")
            _record_tool_span(span_ctx, tool_cls, tool_call, round_num, output)
            return None, output
        try:
            result = await tool.execute(parsed_input)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, TypeError, AssertionError):  # fmt: skip
            raise
        except Exception as exc:
            logger.warning("Tool '%s' failed during execution: %s", snake_name, exc, exc_info=exc)
            output = ToolOutput(content=f"Error: Tool '{snake_name}' failed: {exc}")
            record = ToolCallRecord(tool=tool_cls, input=parsed_input, output=output, round=round_num)
            _record_tool_span(span_ctx, tool_cls, tool_call, round_num, output)
            return record, output

        record = ToolCallRecord(tool=tool_cls, input=parsed_input, output=result, round=round_num)
        _record_tool_span(span_ctx, tool_cls, tool_call, round_num, result)
        return record, result


def _record_tool_span(
    span_ctx: SpanContext,
    tool_cls: type[Tool],
    tool_call: RawToolCall,
    round_num: int,
    output: ToolOutput,
) -> None:
    span_ctx.set_meta(
        tool_name=tool_cls.name,
        tool_class_path=f"{tool_cls.__module__}:{tool_cls.__qualname__}",
        tool_call_id=tool_call.id,
        round_index=round_num,
    )
    span_ctx.set_output_preview(output.model_dump(mode="json"))
    span_ctx._set_output_value(output)


def _parse_tool_arguments(arguments: str) -> Any:
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {"_raw": arguments}
