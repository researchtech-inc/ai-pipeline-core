"""HTTP transport helpers for one LLM attempt."""

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, cast

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from ._config import LLMCoreConfig, get_config, register_on_config_change
from ._defaults import cache_ttl_to_wire
from ._validation import validate_image_content, validate_pdf_content
from .exceptions import PayloadTooLargeError
from .request import AttemptRequest
from .types import ContentPart, CoreMessage, ImageContent, PDFContent, RawToolCall, Role, TextContent

__all__ = [
    "annotate_with_cache_markers",
    "build_extra_body",
    "build_request_headers",
    "compute_cache_key",
    "core_messages_to_api",
    "get_client",
    "open_stream",
    "override_client",
    "shutdown_client",
    "strip_reasoning_signatures",
    "tenant_user",
]

_REASONING_SIGNATURE_KEYS = frozenset({"thought_signature", "thought_signatures"})
# Documented coupling exception: LiteLLM encodes reasoning signatures into
# ``tool_call.id`` via the ``__thought__`` separator for OpenAI-client
# round-trip compatibility. When stripping signatures for cross-credential
# replay recovery, we must remove the encoded sig from the id so the
# downstream transformer does not re-extract it.
_TOOL_CALL_ID_SIGNATURE_SEPARATOR = "__thought__"

logger = logging.getLogger(__name__)
type APIMessage = dict[str, Any]
_MAX_INLINE_FILE_BYTES = 25_000_000
_MAX_HEADER_VALUE_LEN = 256


@dataclass(slots=True)
class _ClientState:
    client: AsyncOpenAI | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_client_state = _ClientState()  # infrastructure singleton: process-wide HTTP pool, caller-stateless.
_client_override: ContextVar[AsyncOpenAI | None] = ContextVar("_llm_openai_client_override", default=None)


def _build_http_limits(config: LLMCoreConfig) -> httpx.Limits:
    """Build the httpx connection-pool limits from the active config."""
    return httpx.Limits(
        max_connections=config.http_max_connections,
        max_keepalive_connections=config.http_max_keepalive_connections,
        keepalive_expiry=config.http_keepalive_expiry_s,
    )


async def get_client() -> AsyncOpenAI:
    """Return a process-wide AsyncOpenAI client with pooled HTTP connections."""
    override = _client_override.get()
    if override is not None:
        return override
    if _client_state.client is not None:
        return _client_state.client
    async with _client_state.lock:
        if _client_state.client is None:
            config = get_config()
            limits = _build_http_limits(config)
            timeout = httpx.Timeout(connect=10.0, read=600.0, write=120.0, pool=10.0)
            _client_state.client = AsyncOpenAI(
                api_key=config.openai_api_key,
                base_url=config.openai_base_url,
                max_retries=0,
                http_client=httpx.AsyncClient(limits=limits, timeout=timeout),
            )
    return _client_state.client


@contextmanager
def override_client(client: AsyncOpenAI) -> Iterator[None]:
    """Override the OpenAI-compatible client for the current context."""
    token = _client_override.set(client)
    try:
        yield
    finally:
        _client_override.reset(token)


def _invalidate_cached_client() -> None:
    """Drop the cached HTTP client without awaiting close.

    Registered with ``_config.register_on_config_change`` so a fresh
    client is built from the new config on the next ``get_client()``
    after ``configure()`` runs. The previous client is left to garbage
    collection (its connection pool closes lazily); the framework
    caller can ``await shutdown_client()`` first if explicit cleanup is
    required before reconfiguration.
    """
    _client_state.client = None


register_on_config_change(_invalidate_cached_client)


async def shutdown_client() -> None:
    """Close the process-wide client, if it was created."""
    client = _client_state.client
    _client_state.client = None
    if client is None:
        return
    closer = getattr(client, "close", None)
    if callable(closer):
        result = closer()
        if isawaitable(result):
            await result


def core_messages_to_api(
    messages: Sequence[CoreMessage],
    *,
    max_inline_file_total_bytes: int,
) -> list[APIMessage]:
    """Convert CoreMessages to OpenAI-compatible chat messages.

    Assistant messages are emitted whenever they carry committed model output
    OR reasoning carriers (``provider_specific_fields``, ``thinking_blocks``)
    so a reasoning-only turn (Gemini ``thought_signature``, OpenAI Responses
    ``reasoning_items``, Anthropic ``thinking_blocks``) round-trips on the
    next request. Without this, multi-turn reasoning continuity breaks.
    """
    _ensure_total_inline_file_budget(messages, limit=max_inline_file_total_bytes)
    result: list[APIMessage] = []
    for message in messages:
        if message.role == Role.TOOL:
            result.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content if isinstance(message.content, str) else "",
            })
            continue
        if message.tool_calls:
            parts = _content_to_api_parts(message.content)
            content_value = _content_value_for_api(parts)
            tool_calls: list[dict[str, Any]] = []
            for call in message.tool_calls:
                tc_entry: dict[str, Any] = {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function_name, "arguments": call.arguments},
                }
                if call.provider_specific_fields:
                    tc_entry["provider_specific_fields"] = dict(call.provider_specific_fields)
                tool_calls.append(tc_entry)
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": content_value,
                "tool_calls": tool_calls,
            }
            # Always carry message-level provider fields and thinking blocks on
            # tool-call turns — reasoning carriers must round-trip even when the
            # assistant turn has no text content.
            if message.provider_specific_fields:
                entry["provider_specific_fields"] = dict(message.provider_specific_fields)
            _apply_reasoning_items(entry, message)
            if message.thinking_blocks:
                entry["thinking_blocks"] = tuple(dict(block) for block in message.thinking_blocks)
            result.append(entry)
            continue
        parts = _content_to_api_parts(message.content)
        content_value = _content_value_for_api(parts)
        has_reasoning_carriers = message.role == Role.ASSISTANT and bool(
            message.provider_specific_fields or message.thinking_blocks
        )
        if content_value is None and not has_reasoning_carriers:
            continue
        entry = {"role": message.role.value, "content": content_value}
        if message.role == Role.ASSISTANT and message.provider_specific_fields:
            entry["provider_specific_fields"] = dict(message.provider_specific_fields)
            _apply_reasoning_items(entry, message)
        if message.role == Role.ASSISTANT and message.thinking_blocks:
            entry["thinking_blocks"] = tuple(dict(block) for block in message.thinking_blocks)
        result.append(entry)
    return result


def annotate_with_cache_markers(
    api_messages: list[APIMessage],
    context_count: int,
    ttl_minutes: int,
) -> None:
    """Apply one cache_control marker at the cached-prefix boundary.

    No-ops when ``ttl_minutes == 0`` (caching disabled) so the request carries
    no ``cache_control`` keys; ``prompt_cache_key`` is plumbed separately.
    """
    ttl = cache_ttl_to_wire(ttl_minutes)
    if ttl is None or context_count <= 0 or not api_messages:
        return
    boundary = api_messages[min(context_count, len(api_messages)) - 1]
    boundary["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    content = boundary.get("content")
    if isinstance(content, list) and content:
        content[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}


def compute_cache_key(api_messages: Sequence[APIMessage]) -> str:
    """Compute the stable prompt cache key from API messages."""
    payload = json.dumps(api_messages, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_extra_body(req: AttemptRequest, *, prompt_cache_key: str | None) -> dict[str, Any]:
    """Build provider-vendored extra_body values for one attempt."""
    body: dict[str, Any] = {}
    if prompt_cache_key is not None:
        body["prompt_cache_key"] = prompt_cache_key
    if req.attempt_index > 0 or req.call.cache.bypass_response_cache:
        body["cache"] = {"no-cache": True, "no-store": True}
    return body


def _safe_header_value(value: Any, *, max_len: int = _MAX_HEADER_VALUE_LEN) -> str:
    """Strip control characters and truncate so httpx accepts the header value."""
    text = "".join(ch for ch in str(value) if ch >= " " and ch != "\x7f")
    return text.strip()[:max_len]


def _put_header(headers: dict[str, str], name: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and not value):
        return
    safe = _safe_header_value(value)
    if safe:
        headers[name] = safe


def build_request_headers(req: AttemptRequest) -> dict[str, str]:
    """Build proxy-only AIPL correlation and tenant headers for one attempt."""
    if not get_config().aipl_proxy:
        return {}
    headers: dict[str, str] = {"x-aipl-call-id": req.call_id}
    correlation = req.correlation
    if correlation is not None:
        _put_header(headers, "x-aipl-logical-request-id", correlation.logical_request_id)
        _put_header(headers, "x-aipl-attempt-number", correlation.attempt_number)
        _put_header(headers, "x-aipl-attempt-kind", correlation.attempt_kind)
        _put_header(headers, "x-aipl-requested-model", correlation.requested_model)
        if correlation.is_fallback:
            _put_header(headers, "x-aipl-is-fallback", "true")
        _put_header(headers, "x-aipl-prev-call-id", correlation.prev_call_id)
        _put_header(headers, "x-aipl-prev-model", correlation.prev_model)
        _put_header(headers, "x-aipl-prev-deployment-id", correlation.prev_deployment_id)
        _put_header(headers, "x-aipl-prev-failure-class", correlation.prev_failure_class)
    tenant = req.call.tenant
    _put_header(headers, "x-aipl-project", tenant.project or get_config().project or None)
    _put_header(headers, "x-aipl-workload-stage", tenant.workload_stage)
    _put_header(headers, "x-aipl-run-id", tenant.run_id)
    _put_header(headers, "x-aipl-workload-kind", tenant.workload_kind)
    return headers


def tenant_user(req: AttemptRequest) -> str | None:
    """Build the OpenAI ``user`` value used for tenant attribution."""
    tenant = req.call.tenant
    project = _safe_header_value(tenant.project or get_config().project)
    if not project:
        return None
    stage = _safe_header_value(tenant.workload_stage) if tenant.workload_stage else ""
    run_id = _safe_header_value(tenant.run_id) if tenant.run_id else ""
    if run_id:
        return "/".join([project, stage, run_id])
    if stage:
        return "/".join([project, stage])
    return project


@asynccontextmanager
async def open_stream(
    req: AttemptRequest, *, messages: list[APIMessage], api_kwargs: dict[str, Any]
) -> AsyncIterator[Any]:
    """Open a raw streaming response for one attempt.

    Yields:
        Raw OpenAI response wrapper consumed by StreamSession.
    """
    client = await get_client()
    create = cast(Any, client.chat.completions.with_raw_response.create)
    request_kwargs: dict[str, Any] = {
        "model": req.model.name,
        "messages": cast(Iterable[ChatCompletionMessageParam], messages),
        "stream": True,
        "timeout": req.call.retry.timeout_s or req.model.timeout_s,
        **api_kwargs,
    }
    extra_headers = build_request_headers(req)
    if extra_headers:
        request_kwargs["extra_headers"] = extra_headers
    yield await create(**request_kwargs)


def _content_to_api_parts(content: str | ContentPart | tuple[ContentPart, ...]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, TextContent):
        return [{"type": "text", "text": content.text}]
    if isinstance(content, ImageContent):
        return _image_content_to_api_parts(content)
    if isinstance(content, PDFContent):
        return _pdf_content_to_api_parts(content)
    result: list[dict[str, Any]] = []
    for part in content:
        result.extend(_content_to_api_parts(part))
    return result


def _content_value_for_api(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]] | None:
    """Choose the content shape that preserves reasoning signatures.

    Some providers' transformers attach signatures only on bare-string content,
    so a single text part collapses to a string instead of a list.
    """
    if not parts:
        return None
    has_content = any(_part_text(part).strip() or part.get("type") != "text" for part in parts)
    if not has_content:
        return None
    if len(parts) == 1 and parts[0].get("type") == "text":
        text = parts[0].get("text")
        if isinstance(text, str):
            return text
    return parts


def _image_content_to_api_parts(content: ImageContent) -> list[dict[str, Any]]:
    data = bytes(content.data)
    _ensure_inline_file_budget(data, filename=f"image ({content.mime_type})")
    if error := validate_image_content(data):
        logger.warning("Dropping image content from request: %s.", error)
        return []
    payload = base64.b64encode(data).decode("utf-8")
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{content.mime_type};base64,{payload}", "detail": "high"},
        }
    ]


def _pdf_content_to_api_parts(content: PDFContent) -> list[dict[str, Any]]:
    data = bytes(content.data)
    _ensure_inline_file_budget(data, filename=content.filename)
    if error := validate_pdf_content(data):
        logger.warning("Dropping PDF %r from request: %s.", content.filename, error)
        return []
    payload = base64.b64encode(data).decode("utf-8")
    return [
        {
            "type": "file",
            "file": {
                "file_data": f"data:application/pdf;base64,{payload}",
                "filename": content.filename,
                "format": "application/pdf",
            },
        }
    ]


def _apply_reasoning_items(entry: dict[str, Any], message: CoreMessage) -> None:
    if message.role != Role.ASSISTANT or not message.provider_specific_fields:
        return
    reasoning_items = message.provider_specific_fields.get("reasoning_items")
    if isinstance(reasoning_items, list | tuple) and reasoning_items:
        entry["reasoning_items"] = [dict(item) if isinstance(item, Mapping) else item for item in reasoning_items]


def _ensure_inline_file_budget(data: bytes, *, filename: str) -> None:
    size = len(data)
    if size <= _MAX_INLINE_FILE_BYTES:
        return
    raise PayloadTooLargeError(
        f"Inline file {filename!r} is {size} bytes; _llm_core inline file support is capped at "
        f"{_MAX_INLINE_FILE_BYTES} bytes per file. Use a file-upload/URI path for larger documents."
    )


def _ensure_total_inline_file_budget(messages: Sequence[CoreMessage], *, limit: int) -> None:
    total = 0
    for message in messages:
        total += _inline_bytes_in_content(message.content)
    if total <= limit:
        return
    raise PayloadTooLargeError(
        f"Inline multimodal payload is {total} bytes across the request; _llm_core inline support is capped at "
        f"{limit} bytes total for this model path. Use a file-upload/URI path for larger documents."
    )


def _inline_bytes_in_content(content: str | ContentPart | tuple[ContentPart, ...]) -> int:
    if isinstance(content, PDFContent):
        return len(bytes(content.data))
    if isinstance(content, ImageContent):
        return len(bytes(content.data))
    if isinstance(content, tuple):
        return sum(_inline_bytes_in_content(part) for part in content)
    return 0


def _part_text(part: dict[str, Any]) -> str:
    text = part.get("text")
    return text if isinstance(text, str) else ""


def strip_reasoning_signatures(messages: tuple[CoreMessage, ...]) -> tuple[CoreMessage, ...]:
    """Return messages with reasoning-signature carriers removed.

    Drops signature keys from each ASSISTANT message's
    ``provider_specific_fields`` and from every contained
    ``RawToolCall.provider_specific_fields``. Also removes any
    ``__thought__``-encoded signature suffix from tool-call ids (and the
    matching TOOL message ``tool_call_id``) so the encoded sig is not
    re-extracted downstream on the retry.

    Idempotent: a second call returns the same tuple.
    """
    rebuilt: list[CoreMessage] = []
    id_remap: dict[str, str] = {}
    changed = False
    for message in messages:
        if message.role != Role.ASSISTANT:
            rebuilt.append(message)
            continue
        stripped_psf = _without_signature_keys(message.provider_specific_fields)
        stripped_calls, remap_part, calls_changed = _strip_signatures_from_tool_calls(message.tool_calls)
        if stripped_psf is message.provider_specific_fields and not calls_changed:
            rebuilt.append(message)
            continue
        id_remap.update(remap_part)
        changed = True
        rebuilt.append(
            message.model_copy(
                update={
                    "provider_specific_fields": stripped_psf,
                    "tool_calls": stripped_calls,
                }
            )
        )
    if id_remap:
        remapped: list[CoreMessage] = []
        for message in rebuilt:
            if message.role == Role.TOOL and message.tool_call_id in id_remap:
                remapped.append(message.model_copy(update={"tool_call_id": id_remap[message.tool_call_id]}))
            else:
                remapped.append(message)
        rebuilt = remapped
    return tuple(rebuilt) if changed else messages


def _without_signature_keys(psf: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not psf:
        return psf
    if not any(key in psf for key in _REASONING_SIGNATURE_KEYS):
        return psf
    cleaned = {key: value for key, value in psf.items() if key not in _REASONING_SIGNATURE_KEYS}
    return cleaned or None


def _strip_signatures_from_tool_calls(
    tool_calls: tuple[RawToolCall, ...] | None,
) -> tuple[tuple[RawToolCall, ...] | None, dict[str, str], bool]:
    if not tool_calls:
        return tool_calls, {}, False
    rebuilt: list[RawToolCall] = []
    id_remap: dict[str, str] = {}
    changed = False
    for call in tool_calls:
        stripped_psf = _without_signature_keys(call.provider_specific_fields)
        stripped_id = _strip_signature_from_id(call.id)
        if stripped_psf is call.provider_specific_fields and stripped_id == call.id:
            rebuilt.append(call)
            continue
        changed = True
        if stripped_id != call.id:
            id_remap[call.id] = stripped_id
        rebuilt.append(
            call.model_copy(
                update={
                    "id": stripped_id,
                    "provider_specific_fields": stripped_psf,
                }
            )
        )
    return (tuple(rebuilt) if changed else tool_calls), id_remap, changed


def _strip_signature_from_id(tool_call_id: str) -> str:
    """Return the tool-call id with any ``__thought__``-encoded signature suffix removed.

    Idempotent: ids that never carried a signature pass through unchanged.
    """
    return tool_call_id.split(_TOOL_CALL_ID_SIGNATURE_SEPARATOR, 1)[0]
