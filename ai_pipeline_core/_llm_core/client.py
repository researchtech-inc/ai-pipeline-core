"""Primitive LLM client for internal modules.

This module provides low-level generate() and generate_structured() functions.
Expects pre-processed content (images already split/compressed by llm layer).

For app code, use the llm module's Conversation class instead.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from typing import Any, TypeVar

from openai import AsyncOpenAI
from openai.lib.streaming.chat import ChunkEvent, ContentDeltaEvent, ContentDoneEvent
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from ai_pipeline_core._token_estimates import estimate_image_tokens, estimate_message_text_tokens, estimate_pdf_tokens
from ai_pipeline_core.exceptions import EmptyResponseError, LLMError, OutputDegenerationError
from ai_pipeline_core.settings import settings

from ._degeneration import detect_output_degeneration
from ._validation import validate_image_content as _validate_image
from .model_config import get_cache_min_tokens, get_openrouter_provider, supports_stop_sequences
from .model_response import Citation, ModelResponse
from .types import (
    ContentPart,
    CoreMessage,
    ImageContent,
    ModelOptions,
    PDFContent,
    RawToolCall,
    Role,
    TextContent,
    TokenUsage,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Valid finish reasons accepted by downstream consumers
_VALID_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "function_call"})


def _conversation_retry_delay_seconds(*, attempt: int, model_options: ModelOptions) -> int:
    """Return retry delay for one failed conversation attempt.

    ``attempt`` is zero-based and corresponds to the attempt that just failed.
    When ``ModelOptions.retry_delay_seconds`` is set, preserve the historical flat-delay
    behavior. Otherwise use the settings-backed exponential schedule with a hard cap.
    """
    if model_options.retry_delay_seconds is not None:
        return model_options.retry_delay_seconds

    return min(
        settings.conversation_retry_delay_seconds * (settings.conversation_retry_backoff_multiplier**attempt),
        settings.conversation_retry_max_delay_seconds,
    )


def _content_to_api_parts(content: str | ContentPart | tuple[ContentPart, ...]) -> list[dict[str, Any]]:
    """Convert content to OpenAI API format.

    Expects pre-processed images (already split/compressed by llm layer).
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if isinstance(content, TextContent):
        return [{"type": "text", "text": content.text}]

    if isinstance(content, ImageContent):
        if err := _validate_image(content.data):
            logger.warning("Skipping invalid image: %s", err)
            return []

        b64 = base64.b64encode(content.data).decode("utf-8")
        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{content.mime_type};base64,{b64}", "detail": "high"},
            }
        ]

    if isinstance(content, PDFContent):
        # Check if "PDF" is actually text (misnamed file)
        if content.data and b"\x00" not in content.data and not content.data.lstrip().startswith(b"%PDF-"):
            try:
                return [{"type": "text", "text": content.data.decode("utf-8")}]
            except UnicodeDecodeError:
                pass
        # Validate PDF header
        if not content.data or not content.data.lstrip().startswith(b"%PDF-"):
            logger.warning("Skipping invalid PDF: invalid header or empty content")
            return []

        b64 = base64.b64encode(content.data).decode("utf-8")
        return [{"type": "file", "file": {"file_data": f"data:application/pdf;base64,{b64}"}}]

    # Tuple of parts
    result: list[dict[str, Any]] = []
    for part in content:
        result.extend(_content_to_api_parts(part))
    return result


def _messages_to_api(messages: list[CoreMessage]) -> list[ChatCompletionMessageParam]:
    """Convert CoreMessages to OpenAI API format."""
    result: list[ChatCompletionMessageParam] = []
    for msg in messages:
        if msg.role == Role.TOOL:
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id or "",
                "content": msg.content if isinstance(msg.content, str) else "",
            })
            continue

        if msg.tool_calls:
            # Always process content to support multimodal/thinking blocks alongside tool calls.
            # Some providers (Claude) include text alongside tool calls; others (OpenAI) have content=null.
            parts = _content_to_api_parts(msg.content)
            # Set None when parts are empty or only contain a blank text entry (OpenAI requires null)
            has_content = any(p.get("text", "").strip() or p.get("type") != "text" for p in parts) if parts else False
            content_value = parts if has_content else None
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": content_value,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function_name, "arguments": tc.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
            result.append(entry)  # type: ignore[arg-type]
            continue

        parts = _content_to_api_parts(msg.content)
        if parts:  # Skip messages with no valid content
            result.append({"role": msg.role.value, "content": parts})  # type: ignore[arg-type]
    return result


def _apply_cache_control(messages: list[ChatCompletionMessageParam], cache_ttl: str, context_count: int) -> None:
    """Apply cache_control to context messages (first context_count messages)."""
    for message in messages[:context_count]:
        message["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}  # type: ignore[typeddict-unknown-key]
        if isinstance(message.get("content"), list):
            # Also apply to last content item for better cache hits
            message["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}  # type: ignore[typeddict-unknown-key]


def _remove_cache_control(messages: list[ChatCompletionMessageParam]) -> list[ChatCompletionMessageParam]:
    """Remove cache_control directives from messages (for retry after cache errors)."""
    for message in messages:
        if (content := message.get("content")) and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "cache_control" in item:  # pyright: ignore[reportUnnecessaryIsInstance]
                    del item["cache_control"]
        if "cache_control" in message:
            del message["cache_control"]
    return messages


def _compute_cache_key(messages: list[ChatCompletionMessageParam], system_prompt: str | None) -> str:
    """Compute SHA256 cache key for messages."""
    key_data = (system_prompt or "") + json.dumps(messages, sort_keys=True, default=str)
    return hashlib.sha256(key_data.encode()).hexdigest()


def _estimate_token_count(messages: list[ChatCompletionMessageParam]) -> int:
    """Rough estimate of token count for Gemini cache threshold."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_message_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":  # pyright: ignore[reportUnnecessaryIsInstance]
                    total += estimate_message_text_tokens(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") in {"image_url", "file"}:  # pyright: ignore[reportUnnecessaryIsInstance]
                    total += estimate_image_tokens() if part.get("type") == "image_url" else estimate_pdf_tokens()
    return total


def _extract_usage(response: Any) -> TokenUsage:
    """Extract token usage from API response."""
    usage = response.usage
    if not usage:
        return TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    cached = 0
    reasoning = 0

    if prompt_details := getattr(usage, "prompt_tokens_details", None):
        cached = getattr(prompt_details, "cached_tokens", 0) or 0

    if completion_details := getattr(usage, "completion_tokens_details", None):
        reasoning = getattr(completion_details, "reasoning_tokens", 0) or 0

    return TokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _extract_cost(response: Any, header_cost: float | None = None) -> float | None:
    """Extract cost from API response usage, falling back to header cost."""
    if (usage := response.usage) and hasattr(usage, "cost"):
        return float(usage.cost)  # type: ignore[attr-defined]
    return header_cost


def _model_name_to_openrouter_model(model: str) -> str:
    """Convert model name to OpenRouter format if needed."""
    if model == "sonar-pro-search":
        return "perplexity/sonar-pro-search"
    if model.endswith("-search"):
        model = model.replace("-search", ":online")
    if "gemini-3" in model and not model.endswith("-preview"):
        model += "-preview"
    provider = get_openrouter_provider(model)
    if provider:
        return f"{provider}/{model}"
    return model


async def _generate_streaming(
    client: AsyncOpenAI,
    model: str,
    messages: list[ChatCompletionMessageParam],
    completion_kwargs: dict[str, Any],
) -> tuple[Any, dict[str, Any], Any]:
    """Execute streaming LLM API call. Returns (response, metadata, stream_usage)."""
    start_time = time.time()
    first_token_time = None
    usage = None

    async with client.chat.completions.stream(
        model=model,
        messages=messages,
        **completion_kwargs,
    ) as stream:
        async for event in stream:
            if isinstance(event, ContentDeltaEvent):
                if not first_token_time:
                    first_token_time = time.time()
            elif isinstance(event, ContentDoneEvent):
                pass
            elif isinstance(event, ChunkEvent) and event.chunk.usage:
                usage = event.chunk.usage

        if not first_token_time:
            first_token_time = time.time()
        response = await stream.get_final_completion()

    metadata = {
        "time_taken": round(time.time() - start_time, 2),
        "first_token_time": round(first_token_time - start_time, 2),
    }
    return response, metadata, usage


async def _generate_non_streaming(
    client: AsyncOpenAI,
    model: str,
    messages: list[ChatCompletionMessageParam],
    completion_kwargs: dict[str, Any],
) -> tuple[Any, dict[str, Any], Any]:
    """Execute non-streaming LLM API call. Returns (response, metadata, None)."""
    start_time = time.time()
    kwargs = {k: v for k, v in completion_kwargs.items() if k != "stream_options"}

    response_format = kwargs.get("response_format")
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        raw = await client.chat.completions.with_raw_response.parse(
            model=model,
            messages=messages,
            **kwargs,
        )
    else:
        raw = await client.chat.completions.with_raw_response.create(
            model=model,
            messages=messages,
            stream=False,
            **kwargs,
        )

    response = raw.parse()

    # Extract cost from x-litellm-response-cost header as fallback
    header_cost: float | None = None
    if raw_cost := raw.headers.get("x-litellm-response-cost"):
        with contextlib.suppress(ValueError, TypeError):
            header_cost = float(raw_cost)

    elapsed = round(time.time() - start_time, 2)
    metadata = {"time_taken": elapsed, "first_token_time": elapsed, "header_cost": header_cost}
    return response, metadata, None


def _build_model_response(
    response: Any,
    metadata: dict[str, Any],
    stream_usage: Any,
    model: str,
    response_format: type[BaseModel] | None,
) -> ModelResponse[Any]:
    """Build ModelResponse from raw API response. Raises ValueError/ValidationError on failure."""
    if not response.choices:
        raise EmptyResponseError(f"LLM provider returned no choices for model={model} — response.choices is {'None' if response.choices is None else 'empty'}.")
    # Normalize response to fix provider bugs
    for choice in response.choices:
        if hasattr(choice.message, "role") and choice.message.role != "assistant":
            object.__setattr__(choice.message, "role", "assistant")
        if choice.finish_reason not in _VALID_FINISH_REASONS:
            object.__setattr__(choice, "finish_reason", "stop")

    content = response.choices[0].message.content or ""
    if "</think>" in content:
        content = content.split("</think>")[-1].strip()

    reasoning_content = ""
    msg = response.choices[0].message
    if rc := getattr(msg, "reasoning_content", None):
        reasoning_content = rc
    elif "</think>" in (msg.content or ""):
        reasoning_content = (msg.content or "").split("</think>")[0].strip()

    # Extract tool calls from response
    raw_tool_calls: tuple[RawToolCall, ...] = ()
    if msg_tool_calls := getattr(msg, "tool_calls", None):
        raw_tool_calls = tuple(
            RawToolCall(id=tc.id or f"call_{i}_{tc.function.name}", function_name=tc.function.name, arguments=tc.function.arguments or "{}")
            for i, tc in enumerate(msg_tool_calls)
        )

    thinking_blocks: tuple[dict[str, Any], ...] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    if hasattr(msg, "thinking_blocks") and msg.thinking_blocks:
        thinking_blocks = tuple(tb if isinstance(tb, dict) else tb.__dict__ for tb in msg.thinking_blocks)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if hasattr(msg, "provider_specific_fields") and msg.provider_specific_fields:
        provider_specific_fields = dict(msg.provider_specific_fields)

    usage = _extract_usage(response)
    if stream_usage:
        usage = TokenUsage(
            prompt_tokens=stream_usage.prompt_tokens,
            completion_tokens=stream_usage.completion_tokens,
            total_tokens=stream_usage.total_tokens,
            cached_tokens=usage.cached_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )
    cost = _extract_cost(response, header_cost=metadata.get("header_cost"))

    if not content and not raw_tool_calls:
        if provider_specific_fields:
            logger.info(
                "Empty response content but provider_specific_fields present (keys: %s). Provider may use non-standard response fields. Model: %s",
                list(provider_specific_fields.keys()),
                model,
            )
        raise EmptyResponseError(f"Empty response content from model={model} — no text and no tool calls.")

    parsed: Any = content
    # Skip structured parsing when tool calls are present (intermediate tool round)
    if response_format is not None and not raw_tool_calls:
        parsed = response_format.model_validate_json(content)

    citations: tuple[Citation, ...] = ()
    if annotations := getattr(response.choices[0].message, "annotations", None):
        url_citations = [a for a in annotations if getattr(a, "type", None) == "url_citation" and a.url_citation]
        citations = tuple(
            Citation(title=a.url_citation.title, url=a.url_citation.url, start_index=a.url_citation.start_index, end_index=a.url_citation.end_index)
            for a in url_citations
        )

    return ModelResponse[Any](
        content=content,
        parsed=parsed,
        reasoning_content=reasoning_content,
        citations=citations,
        usage=usage,
        cost=cost,
        model=model,
        response_id=response.id or "",
        metadata=metadata,
        tool_calls=raw_tool_calls,
        thinking_blocks=thinking_blocks,
        provider_specific_fields=provider_specific_fields,
    )


async def _generate_impl(
    messages: list[CoreMessage],
    *,
    model: str,
    model_options: ModelOptions,
    response_format: type[BaseModel] | None = None,
    purpose: str | None = None,
    expected_cost: float | None = None,
    context_count: int = 0,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, str] | None = None,
) -> ModelResponse[Any]:
    """Shared LLM generation with single retry loop. Handles both text and structured output."""
    if not messages:
        raise ValueError("messages must not be empty")
    if not model:
        raise ValueError("model must be provided")
    # Inject system_prompt as first system message if provided
    effective_messages = list(messages)
    effective_context_count = context_count
    if model_options.system_prompt:
        system_msg = CoreMessage(role=Role.SYSTEM, content=model_options.system_prompt)
        effective_messages = [system_msg] + effective_messages
        effective_context_count = context_count + 1 if context_count > 0 else 1

    api_messages = _messages_to_api(effective_messages)

    if "openrouter" in settings.openai_base_url.lower():
        model = _model_name_to_openrouter_model(model)

    # Apply caching
    cache_ttl = model_options.cache_ttl
    if cache_ttl and effective_context_count > 0:
        min_tokens = get_cache_min_tokens(model)
        if min_tokens > 0 and _estimate_token_count(api_messages[:effective_context_count]) < min_tokens:
            cache_ttl = None
            logger.debug("Disabling cache: context tokens below model minimum (%d)", min_tokens)
        if cache_ttl:
            _apply_cache_control(api_messages, cache_ttl, effective_context_count)

    completion_kwargs: dict[str, Any] = {**model_options.to_openai_completion_kwargs()}
    if "stop" in completion_kwargs and not supports_stop_sequences(model):
        del completion_kwargs["stop"]
    if response_format is not None:
        completion_kwargs["response_format"] = response_format
    if tools:
        completion_kwargs["tools"] = tools
    if tool_choice is not None:
        completion_kwargs["tool_choice"] = tool_choice
    if cache_ttl and effective_context_count > 0:
        completion_kwargs["prompt_cache_key"] = _compute_cache_key(api_messages[:effective_context_count], model_options.system_prompt)

    retries = model_options.retries if model_options.retries is not None else settings.conversation_retries
    total_attempts = 1 + retries
    for attempt in range(total_attempts):
        try:
            async with AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url) as client:
                if model_options.stream:
                    response, metadata, stream_usage = await _generate_streaming(client, model, api_messages, completion_kwargs)
                else:
                    response, metadata, stream_usage = await _generate_non_streaming(client, model, api_messages, completion_kwargs)

                model_response = _build_model_response(response, metadata, stream_usage, model, response_format)

                if expected_cost is not None or purpose:
                    metadata_update: dict[str, Any] = dict(model_response.metadata)
                    if expected_cost is not None:
                        metadata_update["expected_cost"] = expected_cost
                    if purpose:
                        metadata_update["purpose"] = purpose
                    model_response = model_response.model_copy(update={"metadata": metadata_update})

                if model_response.has_tool_calls and not tools:
                    raise ValueError(
                        "Model returned tool calls even though no tools were provided. Pass tools=[...] to Conversation.send() only when tool use is allowed."
                    )

                # Detect output degeneration (token repetition loops) — skip for tool calls and structured output
                if not model_response.has_tool_calls and response_format is None and (explanation := detect_output_degeneration(model_response.content)):
                    raise OutputDegenerationError(
                        f"model={model}, tokens={model_response.usage.completion_tokens}, content_length={len(model_response.content)}: {explanation}"
                    )

                return model_response

        except TimeoutError as timeout_exc:
            logger.warning("LLM generation timeout (attempt %d/%d)", attempt + 1, total_attempts, exc_info=timeout_exc)
            if attempt == total_attempts - 1:
                raise LLMError("Exhausted all retry attempts for LLM generation.") from timeout_exc
        except Exception as e:
            if isinstance(e, ValidationError):
                logger.warning("Structured output validation failed (attempt %d/%d): %s", attempt + 1, total_attempts, e, exc_info=e)
                final_error_msg = f"Structured output validation failed after {total_attempts} attempts"
            elif isinstance(e, EmptyResponseError):
                logger.warning("Empty LLM response (attempt %d/%d, retrying with cache disabled): %s", attempt + 1, total_attempts, e, exc_info=e)
                final_error_msg = f"Empty response content after {total_attempts} attempts."
            else:
                logger.warning("LLM generation failed (attempt %d/%d): %s", attempt + 1, total_attempts, e, exc_info=e)
                final_error_msg = "Exhausted all retry attempts for LLM generation."

            completion_kwargs.setdefault("extra_body", {})
            completion_kwargs["extra_body"]["cache"] = {"no-cache": True}
            completion_kwargs.pop("prompt_cache_key", None)
            api_messages = _remove_cache_control(api_messages)

            if attempt == total_attempts - 1:
                raise LLMError(final_error_msg) from e

        await asyncio.sleep(_conversation_retry_delay_seconds(attempt=attempt, model_options=model_options))

    raise LLMError("Unknown error occurred during LLM generation.")


async def generate(
    messages: list[CoreMessage],
    *,
    model: str,
    model_options: ModelOptions | None = None,
    purpose: str | None = None,
    expected_cost: float | None = None,
    context_count: int = 0,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, str] | None = None,
) -> ModelResponse[str]:
    """Primitive LLM generation — no Document dependency.

    Layer 1 function for internal modules. App code should use llm.Conversation instead.
    """
    return await _generate_impl(
        messages,
        model=model,
        model_options=model_options or ModelOptions(),
        purpose=purpose,
        expected_cost=expected_cost,
        context_count=context_count,
        tools=tools,
        tool_choice=tool_choice,
    )


async def generate_structured(
    messages: list[CoreMessage],
    response_format: type[T],
    *,
    model: str,
    model_options: ModelOptions | None = None,
    purpose: str | None = None,
    expected_cost: float | None = None,
    context_count: int = 0,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, str] | None = None,
) -> ModelResponse[T]:
    """Primitive structured LLM generation — no Document dependency.

    Layer 1 function for internal modules. App code should use llm.Conversation instead.
    """
    return await _generate_impl(
        messages,
        model=model,
        model_options=model_options or ModelOptions(),
        response_format=response_format,
        purpose=purpose,
        expected_cost=expected_cost,
        context_count=context_count,
        tools=tools,
        tool_choice=tool_choice,
    )
