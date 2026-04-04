"""Conversation message rendering with XML-preserving truncation."""

from __future__ import annotations

import json
import re
from typing import Any

from ai_pipeline_core.database._types import SpanRecord
from trace_inspector._helpers import parse_json_object, safe_json
from trace_inspector._truncation import binary_notice, should_inline_document, truncate_content
from trace_inspector._types import LoadedAttachment, LoadedDocument, RenderConfig

__all__ = [
    "render_message_sequence",
]

DOCUMENT_BLOCK_RE = re.compile(r"(?s)<document>.*?</document>")
CONTENT_BLOCK_RE = re.compile(r"(?s)(<content>\n?)(.*?)(\n?</content>)")
ID_BLOCK_RE = re.compile(r"<id>([^<]+)</id>")
ATTACHMENT_BLOCK_RE = re.compile(r"(?s)(<attachment(?: [^>]*)?>)(.*?)(</attachment>)")
ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def render_message_sequence(
    llm_rounds: tuple[SpanRecord, ...],
    tool_calls: tuple[SpanRecord, ...],
    documents_by_short_id: dict[str, LoadedDocument],
    config: RenderConfig,
) -> str:
    """Render an LLM conversation transcript from recorded request/response messages."""
    lines: list[str] = []
    seen_document_turns: dict[str, int] = {}
    rendered_message_count = 0
    tool_calls_by_round = _group_tool_calls_by_round(tool_calls)
    for turn_index, llm_round in enumerate(llm_rounds, start=1):
        meta = parse_json_object(llm_round.meta_json, context=f"Span {llm_round.span_id}")
        request_messages = meta.get("request_messages", [])
        if not isinstance(request_messages, list):
            continue
        round_index = _round_index(meta, turn_index)
        fresh_messages = request_messages[rendered_message_count:]
        rendered_message_count = len(request_messages)
        for message in fresh_messages:
            role = _message_role(message)
            content = _message_content(message)
            if not content:
                continue
            lines.append(f"#### [{role}]")
            if role == "ASSISTANT":
                lines.append("```text")
                lines.append(content)
                lines.append("```")
                lines.append("")
                continue
            lines.append(_render_message_content(content, documents_by_short_id, config, seen_document_turns, turn_index))
            lines.append("")
        response_content = meta.get("response_content")
        if isinstance(response_content, str) and response_content:
            lines.append("#### [ASSISTANT]")
            lines.append("```text")
            lines.append(response_content)
            lines.append("```")
            lines.append("")
        lines.extend(_render_tool_call_blocks(tool_calls_by_round.get(round_index, ())))
    return "\n".join(line for line in lines if line is not None).strip()


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        raw_role = message.get("role", "user")
        if isinstance(raw_role, dict):
            raw_role = raw_role.get("value") or raw_role.get("name") or "user"
        if isinstance(raw_role, str):
            return raw_role.upper()
    return "USER"


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return _flatten_content_parts(content)
    return ""


def _render_message_content(
    content: str,
    documents_by_short_id: dict[str, LoadedDocument],
    config: RenderConfig,
    seen_document_turns: dict[str, int],
    turn_index: int,
) -> str:
    rendered = content
    for block in DOCUMENT_BLOCK_RE.findall(content):
        rendered = rendered.replace(
            block,
            _render_document_block(block, documents_by_short_id, config, seen_document_turns, turn_index),
            1,
        )
    return rendered


def _render_document_block(
    block: str,
    documents_by_short_id: dict[str, LoadedDocument],
    config: RenderConfig,
    seen_document_turns: dict[str, int],
    turn_index: int,
) -> str:
    id_match = ID_BLOCK_RE.search(block)
    if id_match is None:
        return block
    short_id = id_match.group(1).strip()
    document = documents_by_short_id.get(short_id)
    if document is None:
        return _truncate_xml_content(block, f"{short_id}.md", config, total_size_bytes=len(block.encode("utf-8")))

    if document.text_content is None:
        notice = binary_notice(
            name=document.record.name,
            mime_type=document.record.mime_type or "application/octet-stream",
            size_bytes=document.record.size_bytes,
            filename=f"{document.output_filename} attachments",
        )
        return _ensure_xml_attachments(_replace_xml_content(block, notice), document, config)

    if short_id in seen_document_turns:
        previous_turn = seen_document_turns[short_id]
        rendered = _replace_xml_content(block, f"[Previously shown in Turn {previous_turn} — full content in {document.output_filename}]")
        return _ensure_xml_attachments(_replace_xml_attachments(rendered, document, config), document, config)

    seen_document_turns[short_id] = turn_index
    if should_inline_document(document.record.size_bytes, config):
        rendered = _replace_xml_content(block, document.text_content)
        return _ensure_xml_attachments(_replace_xml_attachments(rendered, document, config), document, config)

    preview = truncate_content(
        document.text_content,
        filename=document.output_filename,
        total_size_bytes=document.record.size_bytes,
        head_chars=config.head_chars,
        tail_chars=config.tail_chars,
    )
    rendered = _replace_xml_content(block, preview)
    return _ensure_xml_attachments(_replace_xml_attachments(rendered, document, config), document, config)


def _truncate_xml_content(block: str, filename: str, config: RenderConfig, *, total_size_bytes: int) -> str:
    match = CONTENT_BLOCK_RE.search(block)
    if match is None:
        return block
    preview = truncate_content(
        match.group(2),
        filename=filename,
        total_size_bytes=total_size_bytes,
        head_chars=config.head_chars,
        tail_chars=config.tail_chars,
    )
    return _replace_xml_content(block, preview)


def _replace_xml_content(block: str, replacement: str) -> str:
    return CONTENT_BLOCK_RE.sub(lambda match: f"{match.group(1)}{replacement}{match.group(3)}", block, count=1)


def _replace_xml_attachments(block: str, document: LoadedDocument, config: RenderConfig) -> str:
    attachment_iter = iter(document.attachments)

    def _replace(match: re.Match[str]) -> str:
        attachment = next(attachment_iter, None)
        attrs = dict(ATTRIBUTE_RE.findall(match.group(1)))
        attachment_name = attrs.get("name") or (attachment.name if attachment is not None else "attachment")
        mime_type = attrs.get("type") or (attachment.mime_type if attachment is not None else "application/octet-stream")
        if attachment is None or attachment.text_content is None:
            replacement = binary_notice(
                name=attachment_name,
                mime_type=mime_type,
                size_bytes=0 if attachment is None else attachment.size_bytes,
                filename=f"{document.output_filename} attachments",
            )
            return f"{match.group(1)}{replacement}{match.group(3)}"
        body = attachment.text_content
        if should_inline_document(attachment.size_bytes, config):
            return f"{match.group(1)}{body}{match.group(3)}"
        truncated = truncate_content(
            body,
            filename=document.output_filename,
            total_size_bytes=attachment.size_bytes,
            head_chars=config.head_chars,
            tail_chars=config.tail_chars,
        )
        return f"{match.group(1)}{truncated}{match.group(3)}"

    return ATTACHMENT_BLOCK_RE.sub(_replace, block)


def _ensure_xml_attachments(block: str, document: LoadedDocument, config: RenderConfig) -> str:
    if not document.attachments or "<attachments>" in block:
        return block
    attachment_lines = ["<attachments>"]
    for attachment in document.attachments:
        body = _attachment_body(attachment, document.output_filename, config)
        attachment_lines.append(f'<attachment name="{attachment.name}" type="{attachment.mime_type}">{body}</attachment>')
    attachment_lines.append("</attachments>")
    return block.replace("</document>", "\n" + "\n".join(attachment_lines) + "\n</document>", 1)


def _group_tool_calls_by_round(tool_calls: tuple[SpanRecord, ...]) -> dict[int, tuple[SpanRecord, ...]]:
    grouped: dict[int, list[SpanRecord]] = {}
    for tool_call in tool_calls:
        meta = parse_json_object(tool_call.meta_json, context=f"Span {tool_call.span_id}")
        round_index = _round_index(meta, 1)
        grouped.setdefault(round_index, []).append(tool_call)
    return {round_index: tuple(grouped[round_index]) for round_index in grouped}


def _round_index(meta: dict[str, Any], fallback: int) -> int:
    value = meta.get("round_index")
    if isinstance(value, int) and value > 0:
        return value
    return fallback


def _render_tool_call_blocks(tool_calls: tuple[SpanRecord, ...]) -> list[str]:
    lines: list[str] = []
    for tool_call in tool_calls:
        meta = parse_json_object(tool_call.meta_json, context=f"Span {tool_call.span_id}")
        tool_name = meta.get("tool_name") or tool_call.name
        lines.extend([
            f"#### [TOOL CALL: {tool_name} @ {str(tool_call.span_id)[:8]}]",
            "```json",
            json.dumps(safe_json(tool_call.input_json), indent=2, sort_keys=True),
            "```",
            "",
            f"#### [TOOL RESULT: {tool_name} @ {str(tool_call.span_id)[:8]}]",
            "```json",
            json.dumps(safe_json(tool_call.output_json), indent=2, sort_keys=True),
            "```",
            "",
        ])
    return lines


def _attachment_body(attachment: LoadedAttachment, document_filename: str, config: RenderConfig) -> str:
    if attachment.text_content is None:
        return binary_notice(
            name=attachment.name,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            filename=f"{document_filename} attachments",
        )
    if should_inline_document(attachment.size_bytes, config):
        return attachment.text_content
    return truncate_content(
        attachment.text_content,
        filename=f"{document_filename} attachments",
        total_size_bytes=attachment.size_bytes,
        head_chars=config.head_chars,
        tail_chars=config.tail_chars,
    )


def _flatten_content_parts(content_parts: list[Any]) -> str:
    rendered_parts: list[str] = []
    for part in content_parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            rendered_parts.append(text)
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, str):
            rendered_parts.append(f"[IMAGE CONTENT — {image_url}]")
        elif isinstance(image_url, dict):
            url = image_url.get("url", "")
            rendered_parts.append(f"[IMAGE CONTENT — {url}]")
    return "\n\n".join(rendered_parts)
