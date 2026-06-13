"""Streaming-first internal LLM client."""

import asyncio
import logging
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
from ._strict_schema import describe_schema_for_prompt, schema_for_list, schema_for_model
from ._watchdog import StreamWatchdog, WatchdogConfig
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
    StructuredRepairExhaustedError,
    TerminalError,
)
from .model_response import AttemptOutcome, ModelResponse
from .request import AttemptRequest, ListOf, LLMRequest, ResponseSpec
from .types import AIModel, CoreMessage, ImageContent, PDFContent, Role, TextContent

__all__ = ["generate"]

logger = logging.getLogger(__name__)

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
_DIRECT_PROVIDER_MODEL_UNAVAILABLE_STATUS = 404


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

    preflight = _preflight_capability_outcome(ctx, model)
    if preflight is not None:
        ctx.absorb(preflight)
        return preflight

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
        ctx.absorb(outcome, attempt)
        last_outcome = outcome
        if outcome.response is not None:
            if strip_signatures:
                outcome = _stamp_reasoning_replay_stripped(outcome)
            return outcome
        if outcome.advance_to_fallback or ctx.skip.exhausted:
            return outcome
        if attempt_index < retries:
            retry_after = last_outcome.headers.retry_after_s if last_outcome.headers is not None else None
            await _sleep_retry(attempt_index, ctx.request.retry.retry_delay_seconds, retry_after)
    wrapped = _maybe_wrap_repair_exhausted(last_outcome, model)
    if wrapped is not last_outcome and wrapped.error is not None:
        # Promote the wrapped terminal exception so chain-exhaustion in generate()
        # surfaces ``StructuredRepairExhaustedError`` instead of the raw inner
        # validation error. Bypassing ``ctx.absorb`` here avoids re-running the
        # headers / skip-list / demotion bookkeeping the inner attempt already did.
        ctx.last_error = wrapped.error
    # Retry budget for this model is fully consumed without a usable response.
    # When the last failure class is transient (watchdog, server, rate, timeout,
    # midstream, limiter), advancing to ``AIModel.fallback`` is the right next
    # step — otherwise a model whose every deployment systematically fails the
    # same way (e.g. repeated watchdog kills on PDF + heavy reasoning) burns
    # the budget and raises without ever consulting the fallback. Auth /
    # policy stay on the model: a misconfigured credential or refused content
    # will not be fixed by switching to a sibling AIModel.
    if (
        wrapped.response is None
        and not wrapped.advance_to_fallback
        and wrapped.error is not None
        and _aipl.route(_aipl.classify(wrapped.error, headers=wrapped.headers)).advance_after_exhaustion
    ):
        wrapped = replace(wrapped, advance_to_fallback=True)
    return wrapped


def _preflight_capability_outcome(ctx: _CallContext, model: AIModel) -> AttemptOutcome | None:
    """Run the per-model capability gate before any HTTP attempt.

    Returns an outcome with ``advance_to_fallback=True`` when the request needs a
    capability the model does not declare, so the outer generate loop can move
    to the next fallback hop transparently instead of raising mid-chain. Returns
    ``None`` when the gate passes.
    """
    probe = ctx.build_attempt(model, 0)
    try:
        _check_capabilities(probe)
    except TerminalError as exc:
        return AttemptOutcome(error=exc, advance_to_fallback=True)
    return None


def _maybe_wrap_repair_exhausted(outcome: AttemptOutcome, model: AIModel) -> AttemptOutcome:
    """Wrap a final validation error as ``StructuredRepairExhaustedError``.

    Triggered when the per-model retry loop has exhausted all attempts and the
    final error is a structured-output validation failure. Sets
    ``advance_to_fallback=True`` so the outer ``generate`` loop tries the next
    AIModel in the fallback chain instead of returning a half-formed result.
    Emits a one-shot warning when ``model.supports_json_schema=True`` flagged
    the model as schema-native but the model could not deliver conformant JSON
    even after framework retries — the actionable fix is to flip the flag to
    False so future calls inject the prose schema description automatically.
    """
    error = outcome.error
    if not isinstance(error, LLMValidationError) or isinstance(error, ProviderReasoningReplayError):
        return outcome
    if isinstance(error, StructuredRepairExhaustedError):
        return replace(outcome, advance_to_fallback=True)
    wrapped = StructuredRepairExhaustedError(
        f"Structured-output repair loop exhausted for model={model.name!r}: {error}",
        original=error,
    )
    if model.supports_json_schema:
        _maybe_warn_misset_flag(model, outcome.failed_deployment_id)
    return replace(outcome, error=wrapped, advance_to_fallback=True)


def _maybe_warn_misset_flag(model: AIModel, deployment_id: str | None) -> None:
    """Emit a WARNING when a strict-flagged model keeps producing invalid JSON.

    Fires on the repair-exhausted code path only (per-call frequency: at most
    one per failed structured generation), so logging unconditionally here is
    safe — operators get the actionable message every time the misconfiguration
    consumes a fallback hop.
    """
    logger.warning(
        "AIModel(name=%r, supports_json_schema=True) deployment=%s repeatedly produced "
        "invalid JSON despite native-schema framing. Either the deployment does not honor "
        "strict json_schema (the AIPL proxy may have downgraded it), or the model itself "
        "ignores the schema. Set supports_json_schema=False on this AIModel so the framework "
        "appends the prose schema description to the last USER message automatically.",
        model.name,
        deployment_id or "<unknown>",
    )


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
        deployment_id = _aipl.deployment_id_from(headers) if headers is not None else _aipl.deployment_id_from(exc)
        if action.terminal_for_model or (headers is not None and _aipl.is_group_exhausted(headers)):
            return AttemptOutcome(
                error=exc,
                headers=headers,
                failed_deployment_id=deployment_id,
                advance_to_fallback=True,
            )
        # Unclassified provider 400: the proxy's ``_refine_failure_class``
        # pattern set (see ``aipl_filter.py``) does not cover every OpenRouter
        # / direct-provider 400 phrasing, and non-AIPL deployments stamp no
        # class at all. Treat an unclassified 400 as terminal-for-model when
        # the caller has configured a fallback — the chain is the caller's
        # opt-in that "try the next model" is the right next step. Single-model
        # deployments still fail fast so misconfigured callers see the error.
        if req.model.fallback is not None:
            return AttemptOutcome(
                error=exc,
                headers=headers,
                failed_deployment_id=deployment_id,
                advance_to_fallback=True,
            )
        raise TerminalError(f"Terminal LLM provider error for model={req.model.name}: {exc}") from exc
    except _TRANSPORT_FAILURES + _SEMANTIC_FAILURES as exc:
        await _aipl.maybe_fetch_trace(exc, call_id=req.call_id)
        outcome = _classify_failure(exc)
        if _should_advance_direct_provider_fallback(req, exc, outcome):
            return replace(outcome, advance_to_fallback=True)
        return outcome


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
    generation = req.call.generation
    aipl_proxy = get_config().aipl_proxy
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

    prompt_cache_key: str | None = None
    kwargs: dict[str, Any] = {
        "messages": api_messages,
        "stream_options": {"include_usage": True},
    }
    if aipl_proxy:
        prompt_cache_key = req.call.cache.key_override or (
            _transport.compute_cache_key(api_messages[:cache_prefix_len]) if cache_prefix_len > 0 else None
        )
        cache_ttl = req.call.cache.ttl
        if cache_ttl > 0 and cache_prefix_len > 0:
            _transport.annotate_with_cache_markers(api_messages, cache_prefix_len, cache_ttl)

        metadata = _aipl.build_aipl_metadata(req)
        if prompt_cache_key is not None:
            metadata["prompt_cache_key"] = prompt_cache_key
        kwargs["metadata"] = metadata
        extra_body = _transport.build_extra_body(req, prompt_cache_key=prompt_cache_key)
        if extra_body:
            kwargs["extra_body"] = extra_body
    user = _transport.tenant_user(req)
    if user:
        kwargs["user"] = user
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
    response_format = req.call.response.format
    if response_format is None or _forced_tool_call_requested(req):
        return messages
    messages = _ensure_structured_system_prompt(messages)
    if not req.model.supports_json_schema:
        messages = _append_schema_to_last_user(messages, response_format)
    return messages


def _ensure_structured_system_prompt(messages: tuple[CoreMessage, ...]) -> tuple[CoreMessage, ...]:
    """Merge or prepend the structured-output base SYSTEM prompt."""
    leading = messages[0] if messages else None
    if leading is None or leading.role != Role.SYSTEM or not isinstance(leading.content, str):
        return _with_structured_system_instruction(messages, _STRUCTURED_BASE_SYSTEM_PROMPT)
    if _STRUCTURED_RETRY_SYSTEM_PROMPT in leading.content:
        return messages
    return _with_structured_system_instruction(messages, _STRUCTURED_BASE_SYSTEM_PROMPT)


def _append_schema_to_last_user(
    messages: tuple[CoreMessage, ...],
    response_format: type[BaseModel] | ListOf,
) -> tuple[CoreMessage, ...]:
    """Append a prose schema description to the last USER message.

    Used when ``AIModel.supports_json_schema=False`` so models that do not
    honor strict ``json_schema`` (or that the AIPL proxy has downgraded to
    ``json_object``) still produce conformant JSON. The schema description
    lives only at the transport boundary — ``req.call.messages`` stays frozen,
    so retries and repair attempts rebuild the prompt fresh each time without
    stacking earlier injections.
    """
    if not messages:
        return messages
    schema_text = describe_schema_for_prompt(response_format)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != Role.USER:
            continue
        new_content = _content_with_appended_text(message.content, schema_text)
        new_message = message.model_copy(update={"content": new_content})
        return (*messages[:index], new_message, *messages[index + 1 :])
    return (*messages, CoreMessage(role=Role.USER, content=schema_text))


def _content_with_appended_text(
    content: str | TextContent | ImageContent | PDFContent | tuple[TextContent | ImageContent | PDFContent, ...],
    text: str,
) -> str | tuple[TextContent | ImageContent | PDFContent, ...]:
    """Append text to a CoreMessage.content body without disturbing image/PDF parts."""
    if isinstance(content, str):
        return f"{content}\n\n{text}"
    if isinstance(content, tuple):
        return (*content, TextContent(text=text))
    return (content, TextContent(text=text))


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
    timeout_s = req.call.retry.timeout_s
    config = WatchdogConfig(total_wall_seconds=timeout_s) if timeout_s is not None else WatchdogConfig()
    return StreamWatchdog(deployment_id=req.call.routing.force_deployment_id, config=config)


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


def _should_advance_direct_provider_fallback(
    req: AttemptRequest,
    exc: BaseException,
    outcome: AttemptOutcome,
) -> bool:
    if get_config().aipl_proxy or req.model.fallback is None or outcome.advance_to_fallback:
        return False
    return _aipl.extract_status(exc) == _DIRECT_PROVIDER_MODEL_UNAVAILABLE_STATUS


def _is_signature_replay_error(exc: BaseException) -> bool:
    """True when an upstream 400 rejects a replayed reasoning signature."""
    if _SIGNATURE_REPLAY_PATTERN.search(str(exc)):
        return True
    response = getattr(exc, "response", None)
    body = getattr(response, "text", None) if response is not None else None
    return isinstance(body, str) and bool(_SIGNATURE_REPLAY_PATTERN.search(body))


def _compute_retry_delay(
    attempt_index: int,
    retry_delay_seconds: float | None,
    retry_after_seconds: float | None,
    *,
    jitter: float = 1.0,
) -> float:
    """Resolve the pre-retry sleep, honoring an upstream ``Retry-After`` when present.

    ``jitter`` (0.8..1.2 at runtime) is applied to the exponential/fixed backoff, then the
    ``Retry-After`` floor is enforced *after* jitter so a downward jitter can never make us
    retry before the server's cool-off (the proxy ``aipl_health`` 429/503 path). The floor is
    itself bounded by ``conversation_retry_max_delay_seconds`` so an absurd ``Retry-After``
    (e.g. 9999s) is treated as that maximum rather than honored verbatim and stalling — i.e.
    we never sleep below the server cool-off, but never longer than the configured ceiling.
    """
    config = get_config()
    if retry_delay_seconds is not None:
        base = retry_delay_seconds
    else:
        base = min(
            config.conversation_retry_delay_seconds * (config.conversation_retry_backoff_multiplier**attempt_index),
            config.conversation_retry_max_delay_seconds,
        )
    delay = base * jitter
    if retry_after_seconds is not None and retry_after_seconds > 0:
        floor = min(retry_after_seconds, config.conversation_retry_max_delay_seconds)
        delay = max(delay, floor)
    return delay


async def _sleep_retry(
    attempt_index: int,
    retry_delay_seconds: float | None,
    retry_after_seconds: float | None = None,
) -> None:
    delay = _compute_retry_delay(
        attempt_index,
        retry_delay_seconds,
        retry_after_seconds,
        jitter=_JITTER_RANDOM.uniform(0.8, 1.2),
    )
    if delay > 0:
        await asyncio.sleep(delay)
