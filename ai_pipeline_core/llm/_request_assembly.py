"""Shared LLM request assembly helpers."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import TypeAdapter

from ai_pipeline_core._llm_core import CacheSpec, CoreMessage, GenerationSpec, RetrySpec, Role, RoutingSpec
from ai_pipeline_core._llm_core._routing import derive_workload_id
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import is_list_output_type
from ai_pipeline_core._llm_core.types import AIModel, ModelOptions, TextContent
from ai_pipeline_core._token_estimates import (
    estimate_binary_tokens,
    estimate_image_tokens,
    estimate_message_text_tokens,
    estimate_pdf_tokens,
    estimate_text_tokens,
)
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_task_context

from ._engine import ToolRuntime
from ._request_messages import (
    AnyMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    _document_to_content_parts,
)
from ._substitutor import URLSubstitutor
from ._tool_schema import generate_tool_schema
from .tools import Tool

logger = logging.getLogger(__name__)

_SUBSTITUTOR_INSTRUCTION = (
    "Text uses ... (three dots) to indicate shortened content. "
    "When quoting or referencing shortened URLs, addresses, or identifiers, preserve the ... markers exactly as shown. "
    "Never create shortened content yourself; only reuse shortened content already present in the prompt."
)


def to_core_messages(items: tuple[AnyMessage, ...], model: AIModel) -> list[CoreMessage]:
    """Convert framework messages and documents into core transport messages.

    When rebuilding prior ``ModelResponse`` items into ASSISTANT messages we
    preserve ``provider_specific_fields`` and ``thinking_blocks`` so reasoning
    carriers (Gemini ``thought_signature``, OpenAI Responses ``reasoning_items``,
    Anthropic ``thinking_blocks``) round-trip on multi-turn follow-up calls.
    """
    core_messages: list[CoreMessage] = []
    image_preset = model.vision_preset
    for item in items:
        if isinstance(item, ModelResponse):
            core_messages.append(
                CoreMessage(
                    role=Role.ASSISTANT,
                    content=item.content or "",
                    tool_calls=item.tool_calls or None,
                    provider_specific_fields=item.provider_specific_fields,
                    thinking_blocks=item.thinking_blocks,
                )
            )
        elif isinstance(item, ToolResultMessage):
            core_messages.append(
                CoreMessage(
                    role=Role.TOOL, content=item.content, tool_call_id=item.tool_call_id, name=item.function_name
                )
            )
        elif isinstance(item, AssistantMessage):
            core_messages.append(CoreMessage(role=Role.ASSISTANT, content=item.text))
        elif isinstance(item, UserMessage):
            core_messages.append(CoreMessage(role=Role.USER, content=item.text))
        elif isinstance(item, Document):
            parts = _document_to_content_parts(item, image_preset)
            if parts:
                content = parts[0].text if len(parts) == 1 and isinstance(parts[0], TextContent) else tuple(parts)
                core_messages.append(CoreMessage(role=Role.USER, content=content))
    return core_messages


def prepare_substitutor(
    *,
    context: tuple[Document, ...],
    messages: tuple[AnyMessage, ...],
    enabled: bool,
    model: AIModel,
) -> URLSubstitutor | None:
    """Prepare URL/address substitution for one LLM interaction."""
    if not enabled or model.preserve_input_urls or not model.supports_url_substitution:
        return None
    substitutor = URLSubstitutor()
    items = context + tuple(
        message
        for message in messages
        if isinstance(message, (Document, UserMessage, AssistantMessage, ToolResultMessage))
    )
    substitutor.prepare(collect_text(items))
    return substitutor


async def assemble_api_messages(
    *,
    system_block: str | None,
    context: tuple[Document, ...],
    messages: tuple[AnyMessage, ...],
    model: AIModel,
    substitutor: URLSubstitutor | None,
) -> tuple[list[CoreMessage], int]:
    """Assemble core messages and cache-prefix count for one LLM turn.

    Owns the wire-payload assembly path shared by conversation compatibility
    and ``PromptContract.execute``. Two callers passing the same inputs must
    produce identical messages so the engine's ``prompt_cache_key`` matches
    across the two surfaces.

    The returned ``context_count`` is the number of leading messages that
    form the cacheable prefix. When ``system_block`` is non-empty and at
    least one context message is present, the count is bumped by 1 so the
    cache boundary still lands on the last context message after the SYSTEM
    message is prepended.
    """
    context_core, message_core = await asyncio.to_thread(
        lambda: (to_core_messages(context, model), to_core_messages(messages, model))
    )
    context_count = len(context_core)
    core_messages = context_core + message_core
    if substitutor is not None:
        core_messages = apply_substitution(core_messages, substitutor)
    if system_block:
        core_messages = [CoreMessage(role=Role.SYSTEM, content=system_block), *core_messages]
        if context_count > 0:
            context_count += 1
    return core_messages, context_count


def build_effective_options(
    *,
    model_options: ModelOptions | None,
    current_date: str | None,
    substitutor_active: bool,
    cache_active: bool = False,
) -> ModelOptions | None:
    """Merge per-turn system prompt additions and execution-context cache flags.

    When ``cache_active`` is True (prompt-cache markers will be sent), the
    framework-auto convenience SYSTEM injections (current date, substitutor
    guidance) are suppressed. Vertex AI's cached_content path on Gemini
    rejects requests that combine cached content with any system instruction
    or tool config, so the implicit additions would otherwise cause
    out-of-the-box 400s for users who only enabled caching. User-supplied
    ``model_options.system_prompt`` is preserved as-is — that is the caller's
    explicit choice and may still trigger the provider conflict on Gemini,
    but it is not the framework's place to silently drop it.
    """
    effective = model_options
    if cache_active:
        return effective
    for extra in (
        f"Current date: {current_date}" if current_date else None,
        _SUBSTITUTOR_INSTRUCTION if substitutor_active else None,
    ):
        if not extra:
            continue
        base_prompt = effective.system_prompt if effective else None
        combined = f"{base_prompt}\n\n{extra}" if base_prompt else extra
        effective = (effective or ModelOptions()).model_copy(update={"system_prompt": combined})
    return effective


def generation_overrides(options: ModelOptions | None) -> GenerationSpec | None:
    """Convert ModelOptions into a GenerationSpec override."""
    if options is None:
        return None
    return GenerationSpec(
        temperature=options.temperature,
        reasoning_effort=options.reasoning_effort,
        verbosity=options.verbosity,
        max_completion_tokens=options.max_completion_tokens,
        stop=options.stop,
    )


def retry_overrides(options: ModelOptions | None) -> RetrySpec | None:
    """Convert ModelOptions into a RetrySpec override."""
    if options is None:
        return None
    return RetrySpec(
        retries=options.retries,
        retry_delay_seconds=options.retry_delay_seconds,
        timeout_s=options.timeout,
    )


def cache_overrides(options: ModelOptions | None) -> CacheSpec | None:
    """Convert public options and execution context into cache overrides.

    ``ModelOptions.cache_ttl=None`` means "inherit from AIModel"; return None
    so the engine keeps the AIModel value. Any explicit int (including 0)
    overrides. ``ExecutionContext.disable_cache`` forces ttl=0.
    """
    execution_ctx = get_execution_context()
    disable_cache = execution_ctx is not None and execution_ctx.disable_cache
    ttl_override = options.cache_ttl if options is not None else None
    if disable_cache:
        return CacheSpec(ttl=0)
    if ttl_override is None:
        return None
    return CacheSpec(ttl=ttl_override)


def routing_overrides(purpose: str | None, preferred_deployment_id: str | None = None) -> RoutingSpec:
    """Build routing overrides for one LLM interaction.

    ``preferred_deployment_id`` is a soft hint for multi-turn affinity: pass
    the deployment that handled the previous response so the proxy prefers it
    without forcing the choice when unhealthy.
    """
    execution_ctx = get_execution_context()
    return RoutingSpec(
        workload_id=derive_workload_id(
            run_id=execution_ctx.run_id if execution_ctx is not None else None, name=purpose or "conversation"
        ),
        preferred_deployment_id=preferred_deployment_id,
    )


def tool_runtime(
    tools: list[Tool] | None,
    tool_choice: Literal["auto", "required", "none"] | None,
    max_tool_rounds: int,
    max_calls_by_name: Mapping[str, int] | None = None,
) -> ToolRuntime | None:
    """Build the tool runtime lookup for a turn.

    ``max_calls_by_name`` (when present) caps each tool's per-execution call
    count. Tools not in the mapping run unmetered for callers that have not
    declared per-tool budgets.
    """
    if not tools:
        return None
    lookup: dict[str, Tool] = {}
    for tool in tools:
        name = type(tool).name
        if name in lookup:
            raise ValueError(f"Duplicate tool name '{name}'. Tool names must be unique after snake_case conversion.")
        lookup[name] = tool
    return ToolRuntime(
        schemas=tuple(generate_tool_schema(tool) for tool in tools),
        instances=lookup,
        choice=tool_choice,
        max_rounds=max_tool_rounds,
        max_calls_by_name=dict(max_calls_by_name) if max_calls_by_name else {},
    )


def collect_text(items: tuple[AnyMessage, ...]) -> list[str]:
    """Collect text-bearing conversation items for substitution."""
    texts: list[str] = []
    for item in items:
        if isinstance(item, (UserMessage, AssistantMessage)):
            texts.append(item.text)
        elif isinstance(item, ToolResultMessage):
            texts.append(item.content)
        elif isinstance(item, Document) and item.is_text:
            texts.append(item.text)
    return texts


def apply_substitution(core_messages: list[CoreMessage], substitutor: URLSubstitutor) -> list[CoreMessage]:
    """Apply URL/address substitution to text-bearing core messages."""
    result: list[CoreMessage] = []
    for message in core_messages:
        if isinstance(message.content, str):
            result.append(message.model_copy(update={"content": substitutor.substitute(message.content)}))
            continue
        if isinstance(message.content, tuple):
            parts = [
                TextContent(text=substitutor.substitute(part.text)) if isinstance(part, TextContent) else part
                for part in message.content
            ]
            result.append(message.model_copy(update={"content": tuple(parts)}))
            continue
        result.append(message)
    return result


def restore_response(
    response: ModelResponse[Any],
    substitutor: URLSubstitutor,
    response_format: Any = None,
) -> ModelResponse[Any]:
    """Restore substituted text in final model responses."""
    if substitutor.pattern_count == 0:
        return response
    restored = substitutor.restore(response.content)
    if restored == response.content:
        return response
    update: dict[str, Any] = {"content": restored}
    if response_format is not None and not isinstance(response.parsed, str):
        try:
            if is_list_output_type(response_format):
                update["parsed"] = TypeAdapter(response_format).validate_json(restored)
            else:
                update["parsed"] = response_format.model_validate_json(restored)
        except (ValueError, TypeError) as exc:  # fmt: skip
            logger.warning(
                "URL-restored content failed structured re-parse for %s: %s. "
                "Returning original parsed object with restored content.",
                response_format,
                exc,
            )
            return response.model_copy(update={"content": restored})
    else:
        update["parsed"] = restored
    return response.model_copy(update=update)


def assert_llm_scope(*, source: str) -> None:
    """Reject LLM dispatch from flow scope; ``source`` names the entry point in the error."""
    task_ctx = get_task_context()
    if task_ctx is not None and task_ctx.scope_kind == "flow":
        raise RuntimeError(
            f"{source} called from flow scope without a task. "
            "LLM calls must happen inside a PipelineTask, not directly in PipelineFlow.run()."
        )


def validate_send_scope(*, tools: list[Tool] | None, tool_choice: str | None) -> None:
    """Reject LLM sends from invalid runtime scopes."""
    assert_llm_scope(source="Conversation.send()/send_structured()/send_spec()")
    if tool_choice is not None and not tools:
        raise ValueError(f"tool_choice='{tool_choice}' requires tools=[...] with live Tool instances.")


def approximate_tokens_count(context: tuple[Document, ...], messages: tuple[AnyMessage, ...]) -> int:
    """Approximate token count for all conversation context and messages."""
    total = 0
    for item in context + messages:
        if isinstance(item, ModelResponse):
            total += estimate_message_text_tokens(item.content)
            total += estimate_message_text_tokens(item.reasoning_content) if item.reasoning_content else 0
        elif isinstance(item, ToolResultMessage):
            total += estimate_message_text_tokens(item.content)
        elif isinstance(item, (UserMessage, AssistantMessage)):
            total += estimate_message_text_tokens(item.text)
        elif isinstance(item, Document):
            total += _document_tokens(item)
    return total


def _document_tokens(document: Document) -> int:
    if document.is_text:
        total = estimate_text_tokens(document.text)
    elif document.is_image:
        total = estimate_image_tokens()
    elif document.is_pdf:
        total = estimate_pdf_tokens()
    else:
        total = estimate_binary_tokens()
    for attachment in document.attachments:
        if attachment.is_text:
            total += estimate_text_tokens(attachment.text)
        elif attachment.is_image:
            total += estimate_image_tokens()
        elif attachment.is_pdf:
            total += estimate_pdf_tokens()
        else:
            total += estimate_binary_tokens()
    return total
