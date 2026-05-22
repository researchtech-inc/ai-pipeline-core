"""Build typed ModelResponse objects from streamed completions."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from . import _aipl
from ._degeneration import detect_output_degeneration
from ._strict_schema import resolve_local_ref, schema_for_list, schema_for_model
from .exceptions import (
    ContentPolicyError,
    EmptyResponseError,
    OutputDegenerationError,
    ProseContaminationError,
    StrictModeViolationError,
    StructuredSchemaError,
)
from .model_response import Citation, ModelResponse, StreamCompletion
from .request import AttemptRequest, ListOf
from .types import RawToolCall, TokenUsage

__all__ = ["_build_model_response"]

logger = logging.getLogger(__name__)
_VALID_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "function_call"})
_CONTENT_PREVIEW_CHARS = 500


def _build_model_response(
    completion: StreamCompletion,
    req: AttemptRequest,
    *,
    prompt_cache_key: str | None,
) -> ModelResponse[Any]:
    """Normalize provider output and build one ModelResponse."""
    response = completion.response
    choice = _extract_choice(response, req)
    message = _extract_message(choice, req)
    content = _extract_content(message)
    reasoning = _extract_reasoning(message)
    tool_calls = _extract_tool_calls(message)
    usage = _build_usage(completion)
    finish_reason = _extract_finish_reason(choice)

    if finish_reason == "content_filter":
        headers = {str(key): str(value) for key, value in completion.raw_headers.items()}
        deployment_id = _aipl.deployment_id_from(completion.aipl_headers)
        raise ContentPolicyError(
            f"Provider blocked response for model={req.model.name} due to content policy.",
            deployment_id=deployment_id,
            response_headers=headers,
        )

    provider_fields = _extract_provider_specific_fields(message)
    if not content and not tool_calls and finish_reason != "length" and not _has_reasoning_signal(reasoning, provider_fields, usage):
        raise EmptyResponseError(f"Empty response content from model={req.model.name} — no text, tool calls, or reasoning signal.")

    if req.call.response.format is None and not tool_calls and (explanation := detect_output_degeneration(content)):
        raise OutputDegenerationError(f"model={req.model.name}, tokens={usage.completion_tokens}, content_length={len(content)}: {explanation}")

    cost = _extract_cost(completion)
    parsed_content, parsed = _parse_content(
        content,
        req,
        tool_calls,
        strict_schema=not completion.aipl_headers.response_format_downgraded,
    )
    return ModelResponse[Any](
        content=parsed_content,
        parsed=parsed,
        reasoning_content=reasoning,
        citations=_extract_citations(message, provider_fields),
        usage=usage,
        cost=cost,
        model=req.model.name,
        response_id=str(getattr(response, "id", "") or ""),
        finish_reason=finish_reason,
        transport=_aipl.build_transport_metadata(completion, req, prompt_cache_key=prompt_cache_key, response_cost=cost),
        tool_calls=tool_calls,
        thinking_blocks=_extract_thinking_blocks(message),
        provider_specific_fields=provider_fields,
    )


def _parse_content(
    content: str,
    req: AttemptRequest,
    tool_calls: tuple[RawToolCall, ...],
    *,
    strict_schema: bool = True,
) -> tuple[str, Any]:
    response_format = req.call.response.format
    if response_format is None or tool_calls:
        return content, content
    if isinstance(response_format, ListOf):
        return _parse_list_content(content, response_format.inner, strict_schema=strict_schema)
    return _parse_model_content(content, response_format, strict_schema=strict_schema)


def _parse_list_content(content: str, inner: type[BaseModel], *, strict_schema: bool) -> tuple[str, list[BaseModel]]:
    label = f"list[{inner.__name__}]"
    schema = schema_for_list(inner)
    try:
        payload, _ = _load_json_payload(content, allow_bare_array=True)
        if isinstance(payload, list):
            payload = {"items": payload}
        if not isinstance(payload, dict) or "items" not in payload:
            raise StructuredSchemaError(_schema_error_message(label, content, "expected an object with an 'items' field"))
        if strict_schema:
            _raise_on_strict_extra_keys(payload, schema)
        parsed = TypeAdapter(list[inner]).validate_python(payload["items"])
    except (ProseContaminationError, StrictModeViolationError, StructuredSchemaError):  # fmt: skip
        raise
    except ValidationError as exc:
        raise StructuredSchemaError(_schema_error_message(label, content), original=exc) from exc
    except (ValueError, TypeError) as exc:  # fmt: skip
        raise StructuredSchemaError(_schema_error_message(label, content)) from exc
    array_content = json.dumps([item.model_dump(mode="json") for item in parsed], indent=2)
    return array_content, parsed


def _parse_model_content(content: str, response_format: type[BaseModel], *, strict_schema: bool) -> tuple[str, BaseModel]:
    label = response_format.__name__
    schema = schema_for_model(response_format)
    try:
        payload, normalized = _load_json_payload(content, allow_bare_array=False)
        if not isinstance(payload, dict):
            raise StructuredSchemaError(_schema_error_message(label, content, "expected a JSON object"))
        if strict_schema:
            _raise_on_strict_extra_keys(payload, schema)
        return normalized, response_format.model_validate(payload)
    except (ProseContaminationError, StrictModeViolationError, StructuredSchemaError):  # fmt: skip
        raise
    except ValidationError as exc:
        raise StructuredSchemaError(_schema_error_message(label, content), original=exc) from exc
    except (ValueError, TypeError) as exc:  # fmt: skip
        raise StructuredSchemaError(_schema_error_message(label, content)) from exc


def _schema_error_message(label: str, content: str, detail: str = "failed parsing or validation against the declared Pydantic schema") -> str:
    return f"Structured output for {label} {detail}. Content preview: {content[:_CONTENT_PREVIEW_CHARS]!r}"


def _load_json_payload(content: str, *, allow_bare_array: bool) -> tuple[Any, str]:
    """Strict JSON parsing for structured-output responses.

    Strips a single markdown fence if present, then requires a clean JSON
    payload. Prose/example contamination raises ``ProseContaminationError``,
    which surfaces to the retry path so the repair prompt can correct the
    model on the next attempt.
    """
    stripped = content.strip()
    candidate = _strip_code_fence(stripped)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProseContaminationError(
            "Structured output was not valid JSON; the response contained prose, "
            "markdown, or extraneous text. "
            f"Content preview: {content[:_CONTENT_PREVIEW_CHARS]!r}"
        ) from exc
    if isinstance(payload, list) and not allow_bare_array:
        raise StructuredSchemaError(
            f"Structured output returned a JSON array but the declared schema requires an object. Content preview: {content[:_CONTENT_PREVIEW_CHARS]!r}"
        )
    return payload, _normalized_json(payload)


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return text
    return "\n".join(lines[1:-1]).strip()


def _raise_on_strict_extra_keys(payload: Any, schema: Mapping[str, Any]) -> None:
    extras = _strict_extra_paths(payload, schema, schema, "$")
    if extras:
        raise StrictModeViolationError(f"Provider returned keys outside a strict structured-output schema: {', '.join(extras[:10])}")


def _strict_extra_paths(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> list[str]:
    resolved = resolve_local_ref(schema, root)
    for key in ("anyOf", "oneOf"):
        branches = resolved.get(key)
        if isinstance(branches, list):
            # Only consider branches whose declared ``type`` is structurally
            # compatible with ``value``. A ``{"type": "null"}`` branch in
            # ``Optional[Child]`` would otherwise classify as "no extras"
            # whenever ``value`` is a non-None dict, silently masking real
            # extra keys on the object branch.
            compatible: list[Mapping[str, Any]] = [branch for branch in branches if isinstance(branch, Mapping) and _branch_compatible(value, branch, root)]
            if not compatible:
                # No branch matches the value's shape; Pydantic validation
                # will surface the mismatch — extra-key check is moot here.
                return []
            branch_errors = [_strict_extra_paths(value, branch, root, path) for branch in compatible]
            if any(not errors for errors in branch_errors):
                return []
            return branch_errors[0]
    schema_type = resolved.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        schema_type = non_null[0] if len(non_null) == 1 else None
    if schema_type == "object" and isinstance(value, dict):
        properties = resolved.get("properties")
        props = properties if isinstance(properties, Mapping) else {}
        errors = [f"{path}.{key}" for key in value if key not in props and resolved.get("additionalProperties") is False]
        for key, child_schema in props.items():
            if key in value and isinstance(child_schema, Mapping):
                errors.extend(_strict_extra_paths(value[key], child_schema, root, f"{path}.{key}"))
        return errors
    if schema_type == "array" and isinstance(value, list):
        items = resolved.get("items")
        if isinstance(items, Mapping):
            errors: list[str] = []
            for index, item in enumerate(value):
                errors.extend(_strict_extra_paths(item, items, root, f"{path}[{index}]"))
            return errors
    return []


_JSON_PRIMITIVE_MATCHES: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    ("null", lambda v: v is None),
    ("object", lambda v: isinstance(v, dict)),
    ("array", lambda v: isinstance(v, list)),
    ("string", lambda v: isinstance(v, str)),
    ("boolean", lambda v: isinstance(v, bool)),
    ("integer", lambda v: isinstance(v, int) and not isinstance(v, bool)),
    ("number", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)),
)


def _branch_compatible(value: Any, branch: Mapping[str, Any], root: Mapping[str, Any]) -> bool:
    """Return True when ``branch``'s declared type can structurally hold ``value``.

    Used in ``_strict_extra_paths`` so an ``Optional[Child]`` value of dict
    shape evaluates only the ``Child`` branch, not the ``{"type": "null"}``
    branch (which would falsely pass and mask real extras).
    """
    resolved = resolve_local_ref(branch, root)
    declared = resolved.get("type")
    if isinstance(declared, list):
        types: list[str] = [t for t in declared if isinstance(t, str)]
    elif isinstance(declared, str):
        types = [declared]
    else:
        # Branch did not declare ``type`` (e.g. uses ``$ref`` resolution we
        # couldn't follow, or a constraint-only schema). Treat as compatible.
        return True
    return any(match(value) for type_name, match in _JSON_PRIMITIVE_MATCHES if type_name in types)


def _normalized_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extract_choice(response: Any, req: AttemptRequest) -> Any:
    choices = tuple(getattr(response, "choices", None) or ())
    if not choices:
        raise EmptyResponseError(f"LLM provider returned no choices for model={req.model.name}.")
    return choices[0]


def _extract_message(choice: Any, req: AttemptRequest) -> Any:
    message = getattr(choice, "message", None)
    if message is None:
        raise EmptyResponseError(f"LLM provider returned a null message for model={req.model.name}.")
    return message


def _extract_content(message: Any) -> str:
    return str(getattr(message, "content", "") or "")


def _extract_reasoning(message: Any) -> str:
    reasoning = getattr(message, "reasoning_content", None)
    return reasoning if isinstance(reasoning, str) else ""


def _extract_tool_calls(message: Any) -> tuple[RawToolCall, ...]:
    tool_calls = getattr(message, "tool_calls", None) or ()
    extracted: list[RawToolCall] = []
    for index, tool_call in enumerate(tool_calls):
        function = _get_value(tool_call, "function") or {}
        name = str(_get_value(function, "name") or "")
        arguments = str(_get_value(function, "arguments") or "{}")
        call_id = _get_value(tool_call, "id") or f"call_{index}_{name or 'tool'}"
        extracted.append(
            RawToolCall(
                id=str(call_id),
                function_name=name,
                arguments=arguments,
                provider_specific_fields=_extract_provider_specific_fields(tool_call),
            )
        )
    return tuple(extracted)


def _annotation_to_citation(annotation: Any) -> Citation | None:
    """Normalize one URL citation annotation from dict or SDK object shapes."""
    if _get_value(annotation, "type") != "url_citation":
        return None
    nested = _get_value(annotation, "url_citation")
    source = nested if nested else annotation
    url = _get_value(source, "url")
    if not isinstance(url, str) or not url:
        return None
    title = _get_value(source, "title")
    return Citation(
        title=str(title or ""),
        url=url,
        start_index=_coerce_index(_get_value(source, "start_index")),
        end_index=_coerce_index(_get_value(source, "end_index")),
    )


def _extract_citations(message: Any, provider_specific_fields: Mapping[str, Any] | None = None) -> tuple[Citation, ...]:
    candidates: list[Citation] = []
    for annotation in _as_sequence(_get_value(message, "annotations")):
        citation = _annotation_to_citation(annotation)
        if citation is not None:
            candidates.append(citation)

    fields = provider_specific_fields if isinstance(provider_specific_fields, Mapping) else None
    if not candidates and fields is not None:
        candidates.extend(_grounding_chunk_citations(fields))
        candidates.extend(_url_array_citations(fields))

    return _dedupe_citations(candidates)


def _grounding_chunk_citations(fields: Mapping[str, Any]) -> list[Citation]:
    """Citations from ``grounding_metadata.grounding_chunks[*].web`` shape."""
    grounding = _get_value(fields, "grounding_metadata") or _get_value(fields, "groundingMetadata")
    if not isinstance(grounding, Mapping):
        return []
    chunks = _as_sequence(_get_value(grounding, "grounding_chunks") or _get_value(grounding, "groundingChunks"))
    citations: list[Citation] = []
    for chunk in chunks:
        web = _get_value(chunk, "web") or {}
        if not isinstance(web, Mapping):
            continue
        url = _get_value(web, "uri") or _get_value(web, "url")
        if not isinstance(url, str) or not url:
            continue
        citations.append(Citation(title=str(_get_value(web, "title") or ""), url=url, start_index=0, end_index=0))
    return citations


def _url_array_citations(fields: Mapping[str, Any]) -> list[Citation]:
    """Citations from a flat ``citations: [url, ...]`` provider-specific field."""
    urls = _as_sequence(_get_value(fields, "citations"))
    return [Citation(title="", url=url, start_index=0, end_index=0) for url in urls if isinstance(url, str) and url]


def _dedupe_citations(citations: list[Citation]) -> tuple[Citation, ...]:
    seen: set[tuple[str, str, int, int]] = set()
    unique: list[Citation] = []
    for citation in citations:
        key = (citation.url, citation.title, citation.start_index, citation.end_index)
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return tuple(unique)


def _extract_thinking_blocks(message: Any) -> tuple[dict[str, Any], ...] | None:
    raw_blocks = getattr(message, "thinking_blocks", None)
    if not raw_blocks:
        return None
    return tuple(block if isinstance(block, dict) else dict(getattr(block, "__dict__", {})) for block in raw_blocks)


def _extract_provider_specific_fields(message: Any) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    fields = _get_value(message, "provider_specific_fields")
    if isinstance(fields, Mapping):
        merged.update(dict(fields))
    for key in ("reasoning_items", "reasoning_details", "thought_signatures"):
        value = _get_value(message, key)
        if value:
            merged[key] = value
    return merged or None


def _build_usage(completion: StreamCompletion) -> TokenUsage:
    usage = completion.usage or getattr(completion.response, "usage", None)
    if usage is None:
        return TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    prompt_details = _get_value(usage, "prompt_tokens_details")
    completion_details = _get_value(usage, "completion_tokens_details")
    return TokenUsage(
        prompt_tokens=int(_get_value(usage, "prompt_tokens") or 0),
        completion_tokens=int(_get_value(usage, "completion_tokens") or 0),
        total_tokens=int(_get_value(usage, "total_tokens") or 0),
        cached_tokens=int(_get_value(prompt_details, "cached_tokens") or 0) if prompt_details is not None else 0,
        cache_creation_tokens=int(_get_value(prompt_details, "cache_creation_tokens") or 0) if prompt_details is not None else 0,
        reasoning_tokens=int(_get_value(completion_details, "reasoning_tokens") or 0) if completion_details is not None else 0,
    )


def _extract_cost(completion: StreamCompletion) -> float | None:
    usage = completion.usage or getattr(completion.response, "usage", None)
    cost = _get_value(usage, "cost") if usage is not None else None
    if cost is not None:
        return float(cost)
    response_cost = getattr(completion.aipl_headers, "response_cost", None)
    return float(response_cost) if response_cost is not None else None


def _extract_finish_reason(choice: Any) -> str:
    finish_reason = getattr(choice, "finish_reason", None)
    normalized = str(finish_reason or "stop")
    return normalized if normalized in _VALID_FINISH_REASONS else "stop"


def _has_reasoning_signal(reasoning: str, provider_fields: Mapping[str, Any] | None, usage: TokenUsage) -> bool:
    if reasoning:
        return True
    fields = provider_fields or {}
    if fields.get("reasoning_items") or fields.get("reasoning_details") or fields.get("thought_signatures") or fields.get("chain_of_thought"):
        return True
    return usage.reasoning_tokens > 0


def _get_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    direct = getattr(value, key, None)
    if direct is not None:
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(key)
    return None


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def _coerce_index(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):  # fmt: skip
        return 0
