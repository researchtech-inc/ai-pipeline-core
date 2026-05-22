"""Rendering logic for prompt specifications."""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from ai_pipeline_core.documents import Document

from .spec import PromptSpec, _is_list_field, _is_multi_line_field, _is_previous_message_field, _is_structured_field

logger = logging.getLogger(__name__)

_VOWELS = frozenset("aeiouAEIOU")

_MAX_FIELD_VALUE_LENGTH = 500

_RESULT_TAG = "result"
_RESULT_OPEN = f"<{_RESULT_TAG}>"
_RESULT_CLOSE = f"</{_RESULT_TAG}>"

_RESULT_TAG_RULE = (
    f"Write your complete response inside {_RESULT_OPEN} tags. Do not add any XML tags inside {_RESULT_OPEN}."
)


def _role_sentence(text: str) -> str:
    """Build 'You are a/an {text}.' with correct article."""
    article = "an" if text[0] in _VOWELS else "a"
    return f"You are {article} {text}."


def _pascal_to_title(name: str) -> str:
    """Convert PascalCase class name to title case: RiskAssessmentFramework -> Risk Assessment Framework."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)


def _format_numbered_rule(index: int, text: str) -> str:
    """Format a numbered rule with 3-space indent on continuation lines."""
    return f"{index}. {text}".replace("\n", "\n   ")


def _render_document_listing(items: list[tuple[str, str]]) -> str:
    """Render document listing from (header, description) pairs."""
    blocks: list[str] = []
    for header, desc in items:
        if desc:
            indented = "\n".join(f"  {line}" for line in desc.strip().splitlines())
            blocks.append(f"{header}\n{indented}")
        else:
            blocks.append(header)
    return "Documents provided in context:\n\n" + "\n\n".join(blocks)


def _render_documents_preview(spec_cls: type[PromptSpec[Any]]) -> str:
    """Render document listing from class-level info (for preview / no-documents mode)."""
    items = [
        (
            doc_cls.__name__,
            (doc_cls.__doc__ or "").strip().splitlines()[0] if (doc_cls.__doc__ or "").strip() else "No description",
        )
        for doc_cls in spec_cls.input_documents
    ]
    return _render_document_listing(items)


def _render_documents_actual(documents: Sequence[Document]) -> str:
    """Render document listing from actual Document instances."""
    items = [(f"[{doc.id}] {doc.name}", doc.description) for doc in documents]
    return _render_document_listing(items)


def _is_long_or_multiline(value: str) -> bool:
    """Check whether a field value exceeds the inline limit or contains newlines."""
    return len(value) > _MAX_FIELD_VALUE_LENGTH or "\n" in value


def _warn_auto_promoted(spec_cls: type[PromptSpec], field_name: str, value: str) -> None:
    """Log a warning when a regular Field value is auto-promoted to multi-line treatment."""
    logger.warning(
        "PromptSpec '%s' field '%s' has a long or multiline value (%d chars). "
        "Field parameters are for short, single-line values (up to %d chars). "
        "Use MultiLineField(description='...') for long/multiline content, "
        "or pass it as a Document via input_documents and send_spec(documents=[...]).",
        spec_cls.__name__,
        field_name,
        len(value),
        _MAX_FIELD_VALUE_LENGTH,
    )


def _render_field_value(label: str, value: str) -> str:
    """Render a single inline field value."""
    return f"**{label}:**\n{value}"


def _render_context_fields(spec: PromptSpec) -> list[str]:
    """Render field values for the Context section.

    Multi-line/structured/list fields and auto-promoted regular fields produce a reference
    placeholder; short single-line regular fields are inlined.
    """
    spec_cls = type(spec)
    parts: list[str] = []
    for field_name, field_info in spec_cls.model_fields.items():
        label = field_info.description or field_name
        if _is_list_field(field_info):
            items = getattr(spec, field_name)
            count = len(items) if isinstance(items, (list, tuple)) else 0
            parts.append(f"**{label}:** ({count} items provided in <{field_name}> tags in previous message)")
        elif _is_previous_message_field(field_info):
            parts.append(f"**{label}:** (provided in <{field_name}> tags in previous message)")
        else:
            value = str(getattr(spec, field_name))
            if _is_long_or_multiline(value):
                _warn_auto_promoted(spec_cls, field_name, value)
                parts.append(f"**{label}:** (provided in <{field_name}> tags in previous message)")
            else:
                parts.append(_render_field_value(label, value))
    return parts


def render_text(
    spec: PromptSpec[Any],
    *,
    documents: Sequence[Document] | None = None,
    include_input_documents: bool = True,
) -> str:
    """Render a PromptSpec instance to prompt text.

    Rendering order: Role -> Context -> Task -> Rules -> Guides -> Output Rules -> Output Structure.
    Uses `#` (H1) headers for section boundaries.

    When `documents` is provided, renders actual document instances with their
    runtime id, name, and description. Otherwise falls back to class-level info.
    """
    spec_cls = type(spec)
    sections: list[str] = []

    # 1. Role (skipped when None, e.g. follow-up specs without explicit role)
    if spec_cls.role is not None:
        sections.append(f"# Role\n\n{_role_sentence(spec_cls.role.text)}")

    # 2. Context: document listing + dynamic parameter values
    context_parts: list[str] = []
    if include_input_documents and (documents or spec_cls.input_documents):
        if documents is not None:
            context_parts.append(_render_documents_actual(documents))
        else:
            context_parts.append(_render_documents_preview(spec_cls))

    context_parts.extend(_render_context_fields(spec))

    if context_parts:
        sections.append("# Context\n\n" + "\n\n".join(context_parts))

    # 3. Task
    sections.append(f"# Task\n\n{spec_cls.task}")

    # 4. Rules
    if spec_cls.rules:
        rule_lines = [_format_numbered_rule(i, rule_cls.text) for i, rule_cls in enumerate(spec_cls.rules, 1)]
        sections.append("# Rules\n\n" + "\n".join(rule_lines))

    # 5. Guides (each gets its own section)
    for guide_cls in spec_cls.guides:
        title = _pascal_to_title(guide_cls.__name__)
        content = guide_cls.render().strip()
        # Strip duplicate title if guide template starts with the same title
        content_lines = content.splitlines()
        if content_lines and content_lines[0].strip().lower() == title.lower():
            content = "\n".join(content_lines[1:]).strip()
        sections.append(f"# Reference: {title}\n\n{content}")

    # 6. Output rules (before structure — tell the LLM constraints before format)
    or_lines = [_format_numbered_rule(i, rule_cls.text) for i, rule_cls in enumerate(spec_cls.output_rules, 1)]
    if spec_cls.output_structure is not None:
        or_lines.append(_format_numbered_rule(len(or_lines) + 1, _RESULT_TAG_RULE))
    if or_lines:
        sections.append("# Output Rules\n\n" + "\n".join(or_lines))

    # 7. Output structure
    if spec_cls.output_structure:
        sections.append("# Output Structure\n\n" + spec_cls.output_structure)

    return "\n\n".join(sections)


def _render_structured_value(value: Any) -> str:
    """Render a BaseModel or plain value as JSON with indent=2."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    return json.dumps(value, indent=2, default=str)


def _render_list_xml(field_name: str, items: list[Any] | tuple[Any, ...]) -> str:
    """Render a list field as XML with individually tagged items."""
    parts: list[str] = [f"<{field_name}>"]
    for i, item in enumerate(items, 1):
        tag = f"item_{i}"
        if isinstance(item, BaseModel):
            content = item.model_dump_json(indent=2)
        else:
            content = str(item)
        parts.append(f"<{tag}>\n{content}\n</{tag}>")
    parts.append(f"</{field_name}>")
    return "\n".join(parts)


def _render_multi_line_messages(spec: PromptSpec[Any]) -> list[tuple[str, str]]:  # pyright: ignore[reportUnusedFunction]  # used by conversation.py
    """Return XML-tagged message blocks for multi-line, structured, and list fields.

    Each entry is ``(field_name, xml_block)``.
    Order matches field declaration order on the spec class.

    Includes declared MultiLineFields, StructuredFields, ListFields, and
    regular fields whose values exceed the inline limit (auto-promoted).
    """
    spec_cls = type(spec)
    result: list[tuple[str, str]] = []
    for field_name, field_info in spec_cls.model_fields.items():
        value = getattr(spec, field_name)
        if _is_list_field(field_info):
            items = value if isinstance(value, (list, tuple)) else [value]
            result.append((field_name, _render_list_xml(field_name, items)))
        elif _is_structured_field(field_info):
            rendered = _render_structured_value(value)
            result.append((field_name, f"<{field_name}>\n{rendered}\n</{field_name}>"))
        elif _is_multi_line_field(field_info) or _is_long_or_multiline(str(value)):
            result.append((field_name, f"<{field_name}>{value}</{field_name}>"))
    return result


def render_preview(spec_class: type[PromptSpec[Any]], *, include_input_documents: bool = True) -> str:
    """Render a spec CLASS with placeholder values for dynamic fields.

    Uses `model_construct()` to bypass validation, allowing placeholder strings
    regardless of field type.

    Multi-line fields are shown as XML blocks before the prompt, separated by ``---``.
    """
    placeholders = {field_name: f"{{{field_name}}}" for field_name in spec_class.model_fields}
    instance = spec_class.model_construct(**placeholders)  # pyright: ignore[reportArgumentType] — placeholders are intentionally untyped strings

    # Build multi-line field preview blocks
    ml_blocks: list[str] = []
    for field_name, field_info in spec_class.model_fields.items():
        if _is_previous_message_field(field_info):
            ml_blocks.append(f"<{field_name}>{{{field_name}}}</{field_name}>")

    text = render_text(instance, include_input_documents=include_input_documents)

    if spec_class._follows is not None:
        text = f"[Follows: {spec_class._follows.__name__}]\n\n{text}"

    if ml_blocks:
        return "\n".join(ml_blocks) + "\n\n---\n\n" + text
    return text


_EXTRACT_PATTERN = re.compile(rf"<{_RESULT_TAG}>(.*?)(?:</{_RESULT_TAG}>|$)", re.DOTALL)


def _extract_result(text: str) -> str:  # pyright: ignore[reportUnusedFunction]  # used by conversation.py
    """Extract content from <result> tags. Returns text as-is if no tags found."""
    match = _EXTRACT_PATTERN.search(text)
    return match.group(1).strip() if match else text


__all__ = ["render_preview", "render_text"]
