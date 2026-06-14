"""Shared message and document-content helpers for LLM requests."""

import json
import logging
import re
from dataclasses import dataclass
from html import escape
from typing import Any

from ai_pipeline_core._llm_core import CoreMessage
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import ListOf, get_list_item_type, is_list_output_type
from ai_pipeline_core._llm_core.types import ContentPart, ImageContent, ImagePreset, PDFContent, TextContent
from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.llm._images import validated_binary_parts

from .tools import Tool

__all__ = [
    "AnyMessage",
    "AssistantMessage",
    "ConversationContent",
    "ToolResultMessage",
    "UserMessage",
    "_build_attachment_content",
    "_build_attachment_parts",
    "_core_messages_to_db_span_input",
    "_document_to_content_parts",
    "_document_to_xml_header",
    "_escape_xml_content",
    "_escape_xml_metadata",
    "_normalize_content",
    "_prompt_parts",
    "_response_format_path",
    "_serialize_response_tool_calls",
    "_serialize_tool_config",
]

logger = logging.getLogger(__name__)

ConversationContent = str | Document | list[Document]
_WRAPPER_TAG_RE = re.compile(
    r"<(/?)(document|content|description|attachment|id|name)\b([^>]*)>",
    re.IGNORECASE,
)


def validate_text(data: bytes, name: str) -> str | None:
    """Validate text bytes before wrapping into the LLM XML envelope.

    Document-layer concern (not LLM transport): rejects empty, binary, and
    non-UTF-8 content before XML wrapping. ``_llm_core`` no longer owns text
    validation; only multimodal (image/PDF) validators live in
    ``_llm_core/_validation.py``.
    """
    if not data:
        return f"empty text content in '{name}'"
    if b"\x00" in data:
        return f"binary content (null bytes) in text '{name}'"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        return f"invalid UTF-8 encoding in '{name}': {e}"
    return None


def _escape_xml_metadata(text: str) -> str:
    """Escape metadata for XML text and attribute positions."""
    return escape(text, quote=True)


def _escape_xml_content(text: str) -> str:
    """Escape wrapper-tag-like text so user content cannot break the document envelope."""
    return _WRAPPER_TAG_RE.sub(lambda match: f"&lt;{match.group(1)}{match.group(2)}{match.group(3)}&gt;", text)


def _document_to_xml_header(doc: Document) -> str:
    """Generate XML header for a document with proper escaping."""
    escaped_name = _escape_xml_metadata(doc.name)
    escaped_id = _escape_xml_metadata(doc.id)
    description = f"<description>{_escape_xml_metadata(doc.description)}</description>\n" if doc.description else ""
    return f"<document>\n<id>{escaped_id}</id>\n<name>{escaped_name}</name>\n{description}<content>\n"


def _document_to_content_parts(doc: Document, preset: ImagePreset) -> list[ContentPart]:
    """Convert a Document to content parts for CoreMessage."""
    parts: list[ContentPart] = []
    header = _document_to_xml_header(doc)

    if doc.is_text:
        if err := validate_text(doc.content, doc.name):
            logger.warning(
                "Skipping invalid text document '%s' (validation failed: %s). "
                "Re-create the Document with valid UTF-8 text content before sending it to the LLM.",
                doc.name,
                err,
            )
            return []
        text = _escape_xml_content(doc.content.decode("utf-8"))
        text_fragments = [f"{header}{text}\n"]
        binary_attachment_parts: list[ContentPart] = []

        for attachment in doc.attachments:
            if attachment.is_text:
                attachment_content = _build_attachment_content(attachment)
                if attachment_content:
                    text_fragments.append(attachment_content)
                continue
            binary_attachment_parts.extend(_build_attachment_parts(attachment, preset))

        if binary_attachment_parts:
            parts.append(TextContent(text="".join(text_fragments)))
            parts.extend(binary_attachment_parts)
            parts.append(TextContent(text="</content>\n</document>"))
        else:
            text_fragments.append("</content>\n</document>")
            parts.append(TextContent(text="".join(text_fragments)))
        return parts

    if doc.is_image or doc.is_pdf:
        binary_parts = validated_binary_parts(doc.content, doc.name, is_image=doc.is_image, preset=preset)
        if binary_parts is None:
            return []
        parts.append(TextContent(text=header))
        parts.extend(binary_parts)
        for attachment in doc.attachments:
            parts.extend(_build_attachment_parts(attachment, preset))
        parts.append(TextContent(text="</content>\n</document>"))
        return parts

    logger.warning(
        "Skipping unsupported document '%s' with MIME type '%s'. "
        "Use a text, image, or PDF Document subclass for LLM context.",
        doc.name,
        doc.mime_type,
    )
    return []


def _build_attachment_content(attachment: Any) -> str | None:
    """Build text content for a text attachment."""
    if not attachment.is_text:
        return None
    if err := validate_text(attachment.content, attachment.name):
        logger.warning(
            "Skipping invalid text attachment '%s' (validation failed: %s). "
            "Re-create the attachment with valid UTF-8 text content before sending it to the LLM.",
            attachment.name,
            err,
        )
        return None

    escaped_name = _escape_xml_metadata(attachment.name)
    description_attr = (
        f' description="{_escape_xml_metadata(attachment.description)}"' if attachment.description else ""
    )
    attachment_text = _escape_xml_content(attachment.content.decode("utf-8"))
    return f'<attachment name="{escaped_name}"{description_attr}>\n{attachment_text}\n</attachment>\n'


def _build_attachment_parts(attachment: Any, preset: ImagePreset) -> list[ContentPart]:
    """Build content parts for one attachment."""
    parts: list[ContentPart] = []
    escaped_name = _escape_xml_metadata(attachment.name)
    description_attr = (
        f' description="{_escape_xml_metadata(attachment.description)}"' if attachment.description else ""
    )
    attachment_open = f'<attachment name="{escaped_name}"{description_attr}>\n'

    if attachment.is_text:
        if err := validate_text(attachment.content, attachment.name):
            logger.warning(
                "Skipping invalid text attachment '%s' (validation failed: %s). "
                "Re-create the attachment with valid UTF-8 text content before sending it to the LLM.",
                attachment.name,
                err,
            )
            return []
        attachment_text = _escape_xml_content(attachment.content.decode("utf-8"))
        parts.append(TextContent(text=f"{attachment_open}{attachment_text}\n</attachment>\n"))
        return parts

    if attachment.is_image or attachment.is_pdf:
        binary_parts = validated_binary_parts(
            attachment.content, attachment.name, is_image=attachment.is_image, preset=preset
        )
        if binary_parts is None:
            return []
        parts.append(TextContent(text=attachment_open))
        parts.extend(binary_parts)
        parts.append(TextContent(text="</attachment>\n"))
        return parts

    logger.warning(
        "Skipping unsupported attachment '%s' with MIME type '%s'. "
        "Use a text, image, or PDF attachment for LLM context.",
        attachment.name,
        attachment.mime_type,
    )
    return []


@dataclass(frozen=True, slots=True)
class UserMessage:
    """Internal wrapper for user string messages."""

    text: str


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """Internal wrapper for injected assistant messages."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """Internal wrapper for tool execution results."""

    tool_call_id: str
    function_name: str
    content: str


AnyMessage = Document | ModelResponse[Any] | UserMessage | AssistantMessage | ToolResultMessage


def _normalize_content(content: ConversationContent) -> tuple[Document | UserMessage, ...]:
    """Normalize conversation-compatibility content to Documents or user messages."""
    if isinstance(content, str):
        return (UserMessage(content),)
    if isinstance(content, Document):
        return (content,)
    return tuple(content)


def _core_messages_to_db_span_input(messages: list[CoreMessage]) -> list[dict[str, Any]]:
    """Convert CoreMessages to database span input, replacing binary parts with blob refs."""
    result: list[dict[str, Any]] = []
    for message in messages:
        payload: dict[str, Any] = {"role": message.role.value}
        if isinstance(message.content, str):
            payload["content"] = message.content
        else:
            parts: list[dict[str, str]] = []
            content_parts = message.content if isinstance(message.content, tuple) else (message.content,)
            for part in content_parts:
                if isinstance(part, TextContent):
                    parts.append({"type": "text", "text": part.text})
                    continue
                if isinstance(part, ImageContent):
                    parts.append({
                        "type": "image",
                        "$doc_ref": compute_content_sha256(bytes(part.data)),
                        "media_type": part.mime_type,
                    })
                    continue
                if isinstance(part, PDFContent):
                    parts.append({
                        "type": "pdf",
                        "$doc_ref": compute_content_sha256(bytes(part.data)),
                        "media_type": "application/pdf",
                    })
            payload["content"] = parts
        if message.tool_calls is not None:
            payload["tool_calls"] = [tool_call.model_dump(mode="json") for tool_call in message.tool_calls]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            payload["name"] = message.name
        result.append(payload)
    return result


def _response_format_path(response_format: Any) -> str:
    """Convert a response model class to an importable path.

    For ``list[T]`` output types, returns ``list[module:QualName]``.
    """
    if response_format is None:
        return ""
    if isinstance(response_format, ListOf):
        return f"list[{response_format.inner.__module__}:{response_format.inner.__qualname__}]"
    if is_list_output_type(response_format):
        item_type = get_list_item_type(response_format)
        return f"list[{item_type.__module__}:{item_type.__qualname__}]"
    return f"{response_format.__module__}:{response_format.__qualname__}"


def _prompt_parts(content: ConversationContent) -> tuple[str, tuple[str, ...]]:
    """Return prompt text and prompt document SHA256 values for one send call."""
    if isinstance(content, str):
        return content, ()
    if isinstance(content, Document):
        return "", (content.sha256,)
    return "", tuple(document.sha256 for document in content)


def _serialize_tool_config(tool: Tool) -> dict[str, Any]:
    """Serialize tool metadata for span detail_json."""
    tool_cls = type(tool)
    return {
        "name": tool_cls.name,
        "class_path": f"{tool_cls.__module__}:{tool_cls.__qualname__}",
    }


def _serialize_response_tool_calls(tool_calls: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Serialize tool calls for llm_round detail_json."""
    serialized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        try:
            arguments: Any = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            arguments = {"_raw": tool_call.arguments}
        serialized.append({
            "id": tool_call.id,
            "function_name": tool_call.function_name,
            "arguments": arguments,
        })
    return serialized
