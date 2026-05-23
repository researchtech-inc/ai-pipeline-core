"""Streaming-first internal LLM client."""

import asyncio
import re
from dataclasses import replace
from secrets import SystemRandom
from typing import Any, overload

import httpx
import openai
from pydantic import BaseModel

from . import _aipl, _transport
from ._config import get_config
from ._response_builder import _build_model_response
from ._routing import _CallContext
from ._stream import StreamSession
from ._strict_schema import schema_for_list, schema_for_model
from ._watchdog import StreamWatchdog
from .exceptions import (
    ContentPolicyError,
    EmptyResponseError,
    LLMError,
    LLMValidationError,
    MidStreamProviderError,
    OutputDegenerationError,
    PartialToolCallStreamError,
    PayloadTooLargeError,
    ProviderReasoningReplayError,
    StreamWatchdogError,
    TerminalError,
)
from .model_response import AttemptOutcome, ModelResponse
from .request import AttemptRequest, ListOf, LLMRequest, ResponseSpec
from .types import AIModel, CoreMessage, ImageContent, PDFContent, Role

__all__ = ["generate"]

_TRANSPORT_FAILURES = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APIError,
    httpx.HTTPError,
    TimeoutError,
)
_SEMANTIC_FAILURES = (
    EmptyResponseError,
    OutputDegenerationError,
    ContentPolicyError,
    StreamWatchdogError,
    LLMValidationError,
    MidStreamProviderError,
    PartialToolCallStreamError,
)
_TERMINAL_FAILURES = (openai.BadRequestError, PayloadTooLargeError)
_JITTER_RANDOM = SystemRandom()
_STRUCTURED_BASE_SYSTEM_PROMPT = (
    "Return valid JSON matching the response schema. Do not include prose, markdown, code fences, or explanation."
)
_STRUCTURED_RETRY_SYSTEM_PROMPT = (
    "Return ONLY the JSON value matching the response schema. No prose, no markdown, no code fences, no explanation."
)
_STRUCTURED_SYSTEM_PROMPTS = frozenset({_STRUCTURED_BASE_SYSTEM_PROMPT, _STRUCTURED_RETRY_SYSTEM_PROMPT})
_SIGNATURE_REPLAY_PATTERN = re.compile(
    r"(?:corrupted|invalid|missing|not\s+valid).{0,40}?thought.?signature|thought.?signature.{0,40}?(?:corrupted|invalid|missing|not\s+valid)",
    re.IGNORECASE | re.DOTALL,
)


@overload
async def generate[T: BaseModel](request: LLMRequest, *, response_type: type[T]) -> ModelResponse[T]: ...


@overload
async def generate(request: LLMRequest) -> ModelResponse[str]: ...


async def generate(request: LLMRequest, *, response_type: type[BaseModel] | None = None) -> ModelResponse[Any]:
    """Generate one response, walking retries and AIModel fallbacks."""
    if response_type is not None:
        request = replace(request, response=replace(request.response, format=response_type))

    ctx = _CallContext(request)
    for model in ctx.model_chain():
        outcome = await _try_one_model(ctx, model)
        if outcome.response is not None:
            return outcome.response
        if not outcome.advance_to_fallback:
            break
        # Group exhaustion is per-model: the fallback model has its own
        # routing group and should not inherit the prior model's exhausted
        # flag (otherwise its first transient failure short-circuits its
        # retries via the early-return in _try_one_model).
        ctx.skip.exhausted = False
    terminal = ctx.terminal_error()
    if isinstance(terminal, LLMError):
        raise terminal
    raise LLMError("Exhausted all retry attempts for LLM generation.") from terminal


async def _try_one_model(ctx: _CallContext, model: AIModel) -> AttemptOutcome:
    retries = ctx.request.retry.retries
    if retries is None:
        retries = get_config().conversation_retries

    last_outcome = AttemptOutcome(error=ctx.last_error)
    strip_signatures = False
    stripped_deployment_id: str | None = None
    for attempt_index in range(retries + 1):
        if attempt_index > 0 and isinstance(last_outcome.error, ProviderReasoningReplayError):
            # Latch once a deployment rejects a replayed signature so any
            # subsequent retry on this model (including those triggered by
            # transient transport failures) keeps the stripped history and
            # the pinned deployment id.
            strip_signatures = True
            stripped_deployment_id = last_outcome.error.deployment_id

        attempt = ctx.build_attempt(model, attempt_index)
        if strip_signatures:
            attempt = _with_stripped_reasoning(attempt, stripped_deployment_id)
        elif attempt_index > 0 and isinstance(last_outcome.error, LLMValidationError):
            attempt = _with_structured_retry_prompt(attempt)

        outcome = await _execute_attempt(attempt)
        ctx.absorb(outcome)
        last_outcome = outcome
        if outcome.response is not None:
            if strip_signatures:
                outcome = _stamp_reasoning_replay_stripped(outcome)
            return outcome
        if outcome.advance_to_fallback or ctx.skip.exhausted:
            return outcome
        if attempt_index < retries:
            await _sleep_retry(attempt_index, ctx.request.retry.retry_delay_seconds)
    return last_outcome


def _with_stripped_reasoning(req: AttemptRequest, deployment_id: str | None) -> AttemptRequest:
    """Re-emit an attempt with thought_signature blobs removed and routing pinned.

    The signature rejection always comes from the exact deployment that just
    handled the request, so the retry forces the same deployment to avoid
    landing on another peer that might also reject the (already-stripped) blob.
    """
    new_messages = _transport.strip_reasoning_signatures(req.call.messages)
    new_routing = req.call.routing
    if deployment_id:
        new_routing = replace(new_routing, force_deployment_id=deployment_id)
    new_call = replace(req.call, messages=new_messages, routing=new_routing)
    return replace(req, call=new_call)


def _stamp_reasoning_replay_stripped(outcome: AttemptOutcome) -> AttemptOutcome:
    response = outcome.response
    if response is None:
        return outcome
    new_aipl = replace(response.transport.aipl, reasoning_replay_stripped=True)
    new_transport = replace(response.transport, aipl=new_aipl)
    return replace(outcome, response=response.model_copy(update={"transport": new_transport}))


async def _execute_attempt(req: AttemptRequest) -> AttemptOutcome:
    try:
        api_kwargs, prompt_cache_key = _prepare_api_kwargs(req)
        watchdog = _make_watchdog(req)
        messages = api_kwargs.pop("messages")
        async with (
            _transport.open_stream(req, messages=messages, api_kwargs=api_kwargs) as raw,
            StreamSession(raw, watchdog=watchdog) as session,
        ):
            completion = await session.drain()
        response = _build_model_response(completion, req, prompt_cache_key=prompt_cache_key)
        if req.call.debug.capture_trace:
            trace = await _aipl.fetch_trace(response.transport.aipl.call_id or req.call_id)
            if trace is not None:
                transport = replace(response.transport, aipl=replace(response.transport.aipl, trace=trace))
                response = response.model_copy(update={"transport": transport})
        return AttemptOutcome(response=response, headers=completion.aipl_headers)
    except _TERMINAL_FAILURES as exc:
        if isinstance(exc, PayloadTooLargeError):
            raise
        await _aipl.maybe_fetch_trace(exc, call_id=req.call_id)
        if _is_signature_replay_error(exc):
            headers = _aipl.headers_from_exception(exc)
            deployment_id = _aipl.deployment_id_from(headers) if headers is not None else _aipl.deployment_id_from(exc)
            replay_exc = ProviderReasoningReplayError(
                f"Upstream rejected replayed thought_signature for model={req.model.name}: {exc}",
                original=exc,
                deployment_id=deployment_id,
            )
            return AttemptOutcome(error=replay_exc, headers=headers, failed_deployment_id=deployment_id)
        # The AIPL proxy can stamp ``x-aipl-failure-class`` on a 400 to mark it
        # as terminal for the current model (context_limit / capability_mismatch
        # / refusal) without making it terminal for the whole call. Honor that
        # by advancing the AIModel chain instead of raising TerminalError.
        headers = _aipl.headers_from_exception(exc)
        action = _aipl.route(_aipl.classify(exc, headers=headers))
        if action.terminal_for_model or (headers is not None and _aipl.is_group_exhausted(headers)):
            deployment_id = _aipl.deployment_id_from(headers) if headers is not None else _aipl.deployment_id_from(exc)
            return AttemptOutcome(
                error=exc,
                headers=headers,
                failed_deployment_id=deployment_id,
                advance_to_fallback=True,
            )
        raise TerminalError(f"Terminal LLM provider error for model={req.model.name}: {exc}") from exc
    except _TRANSPORT_FAILURES + _SEMANTIC_FAILURES as exc:
        await _aipl.maybe_fetch_trace(exc, call_id=req.call_id)
        return _classify_failure(exc)


def _check_capabilities(req: AttemptRequest) -> None:
    """Raise TerminalError when the request needs a capability the model does not declare."""
    model = req.model
    if req.call.response.format is not None and not model.supports_structured_output:
        raise TerminalError(
            f"Model {model.name!r} declares supports_structured_output=False but the request "
            "includes a response_format. Use a model that supports structured output."
        )
    if req.call.tools.schemas and not model.supports_tools:
        raise TerminalError(
            f"Model {model.name!r} declares supports_tools=False but the request includes tool schemas. "
            "Use a model that supports tool calling."
        )
    if model.supports_images and model.supports_pdfs:
        return
    for message in req.call.messages:
        for part in _iter_content_parts(message.content):
            if isinstance(part, ImageContent) and not model.supports_images:
                raise TerminalError(
                    f"Model {model.name!r} declares supports_images=False but the request "
                    "includes ImageContent. Use a vision-capable model."
                )
            if isinstance(part, PDFContent) and not model.supports_pdfs:
                raise TerminalError(
                    f"Model {model.name!r} declares supports_pdfs=False but the request "
                    "includes PDFContent. Use a PDF-capable model."
                )


def _iter_content_parts(content: Any) -> Any:
    if isinstance(content, (ImageContent, PDFContent)):
        yield content
        return
    if isinstance(content, tuple):
        for part in content:
            yield from _iter_content_parts(part)


def _prepare_api_kwargs(req: AttemptRequest) -> tuple[dict[str, Any], str | None]:
    _check_capabilities(req)
    generation = req.call.generation
    messages = _messages_for_transport(req)
    api_messages = _transport.core_messages_to_api(
        messages,
        max_inline_file_total_bytes=req.model.max_inline_file_total_bytes,
    )
    # If the structured-output SYSTEM instruction was prepended as a new
    # message (rather than merged into an existing one), shift the cache
    # prefix by one so it still covers the originally cacheable context
    # instead of clipping the last context message.
    cache_prefix_len = req.call.cache.context_count
    if cache_prefix_len > 0 and len(messages) > len(req.call.messages):
        cache_prefix_len += len(messages) - len(req.call.messages)

    prompt_cache_key = req.call.cache.key_override or (
        _transport.compute_cache_key(api_messages[:cache_prefix_len]) if cache_prefix_len > 0 else None
    )

    cache_ttl = req.call.cache.ttl
    if cache_ttl > 0 and cache_prefix_len > 0:
        _transport.annotate_with_cache_markers(api_messages, cache_prefix_len, cache_ttl)

    metadata = _aipl.build_aipl_metadata(req)
    if prompt_cache_key is not None:
        metadata["prompt_cache_key"] = prompt_cache_key
    kwargs: dict[str, Any] = {
        "messages": api_messages,
        "extra_body": _transport.build_extra_body(req, prompt_cache_key=prompt_cache_key),
        "metadata": metadata,
        "stream_options": {"include_usage": True},
    }
    if generation.temperature is not None:
        kwargs["temperature"] = generation.temperature
    if generation.max_completion_tokens is not None:
        kwargs["max_completion_tokens"] = generation.max_completion_tokens
    if generation.reasoning_effort:
        kwargs["reasoning_effort"] = generation.reasoning_effort
    if generation.verbosity:
        kwargs["verbosity"] = generation.verbosity
    if generation.stop and req.model.supports_stop_sequences:
        kwargs["stop"] = list(generation.stop)
    if req.call.tools.schemas:
        kwargs["tools"] = list(req.call.tools.schemas)
    if req.call.tools.choice is not None:
        kwargs["tool_choice"] = req.call.tools.choice
    if req.call.response.format is not None:
        kwargs["response_format"] = _response_format(req.call.response)
    return kwargs, prompt_cache_key


def _with_structured_retry_prompt(req: AttemptRequest) -> AttemptRequest:
    """Add a repair instruction after a structured-output parse failure.

    When the retry instruction is prepended as a fresh SYSTEM message
    (because the original messages had no leading SYSTEM), we also bump
    ``cache.context_count`` by the same delta so the cache marker continues
    to cover the originally cacheable context. Without this, the cache
    prefix accounting in ``_prepare_api_kwargs`` would mark one message
    short of the intended boundary on retry.
    """
    if req.call.response.format is None:
        return req
    new_messages = _with_structured_system_instruction(req.call.messages, _STRUCTURED_RETRY_SYSTEM_PROMPT)
    new_cache = req.call.cache
    prepended_count = len(new_messages) - len(req.call.messages)
    if prepended_count > 0 and req.call.cache.context_count > 0:
        new_cache = replace(req.call.cache, context_count=req.call.cache.context_count + prepended_count)
    call = replace(req.call, messages=new_messages, cache=new_cache)
    return replace(req, call=call)


def _messages_for_transport(req: AttemptRequest) -> tuple[CoreMessage, ...]:
    messages = req.call.messages
    if req.call.response.format is None or _forced_tool_call_requested(req):
        return messages
    leading = messages[0] if messages else None
    if leading is None or leading.role != Role.SYSTEM or not isinstance(leading.content, str):
        return _with_structured_system_instruction(messages, _STRUCTURED_BASE_SYSTEM_PROMPT)
    if _STRUCTURED_RETRY_SYSTEM_PROMPT in leading.content:
        return messages
    return _with_structured_system_instruction(messages, _STRUCTURED_BASE_SYSTEM_PROMPT)


def _with_structured_system_instruction(
    messages: tuple[CoreMessage, ...],
    instruction: str,
) -> tuple[CoreMessage, ...]:
    """Merge ``instruction`` into the leading SYSTEM message, or prepend a new one.

    If a prior structured-output prompt is already embedded, replace it; otherwise
    append. Non-string SYSTEM content (multimodal) is left untouched and the
    instruction is prepended as a fresh SYSTEM message.
    """
    if not messages or messages[0].role != Role.SYSTEM or not isinstance(messages[0].content, str):
        return (CoreMessage(role=Role.SYSTEM, content=instruction), *messages)
    content = messages[0].content
    if instruction in content:
        return messages
    for prompt in _STRUCTURED_SYSTEM_PROMPTS:
        content = content.replace(prompt, "")
    content = content.strip()
    merged = f"{content}\n\n{instruction}" if content else instruction
    return (messages[0].model_copy(update={"content": merged}), *messages[1:])


def _forced_tool_call_requested(req: AttemptRequest) -> bool:
    """True when this request must produce tool calls, not final structured text."""
    if not req.call.tools.schemas:
        return False
    choice = req.call.tools.choice
    if choice == "required":
        return True
    return isinstance(choice, dict) and choice.get("type") == "function"


def _response_format(spec: ResponseSpec) -> dict[str, Any]:
    response_format = spec.format
    assert response_format is not None
    if isinstance(response_format, ListOf):
        name = f"ListOf{response_format.inner.__name__}"
        schema = schema_for_list(response_format.inner)
    else:
        name = response_format.__name__
        schema = schema_for_model(response_format)
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


def _make_watchdog(req: AttemptRequest) -> StreamWatchdog:
    if req.call.debug.disable_watchdog:
        return StreamWatchdog(enabled=False)
    return StreamWatchdog(deployment_id=req.call.routing.force_deployment_id)


def _classify_failure(exc: BaseException) -> AttemptOutcome:
    headers = _aipl.headers_from_exception(exc)
    failure_class = _aipl.classify(exc, headers=headers)
    action = _aipl.route(failure_class)
    deployment_id = _aipl.deployment_id_from(headers) if headers is not None else _aipl.deployment_id_from(exc)
    skip: frozenset[str] = frozenset({deployment_id}) if action.skip_deployment and deployment_id else frozenset()
    return AttemptOutcome(
        error=exc,
        headers=headers,
        new_skip_ids=skip,
        failed_deployment_id=deployment_id,
        demote_workload=action.backoff_workload,
        advance_to_fallback=action.terminal_for_model or (headers is not None and _aipl.is_group_exhausted(headers)),
    )


def _is_signature_replay_error(exc: BaseException) -> bool:
    """True when an upstream 400 rejects a replayed reasoning signature."""
    if _SIGNATURE_REPLAY_PATTERN.search(str(exc)):
        return True
    response = getattr(exc, "response", None)
    body = getattr(response, "text", None) if response is not None else None
    return isinstance(body, str) and bool(_SIGNATURE_REPLAY_PATTERN.search(body))


async def _sleep_retry(attempt_index: int, retry_delay_seconds: float | None) -> None:
    if retry_delay_seconds is None:
        config = get_config()
        delay = min(
            config.conversation_retry_delay_seconds * (config.conversation_retry_backoff_multiplier**attempt_index),
            config.conversation_retry_max_delay_seconds,
        )
    else:
        delay = retry_delay_seconds
    jittered_delay = delay * _JITTER_RANDOM.uniform(0.8, 1.2)
    if jittered_delay > 0:
        await asyncio.sleep(jittered_delay)
