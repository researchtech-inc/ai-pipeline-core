"""Lightweight attachment model for multi-part documents."""

import base64
from functools import cached_property
from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from ai_pipeline_core.documents.exceptions import DocumentNameError

from ._mime_type import (
    detect_mime_type,
    is_image_mime_type,
    is_pdf_mime_type,
    is_text_mime_type,
)
from .utils import _DATA_URI_PATTERN, _serialize_content_bytes

__all__ = [
    "Attachment",
]


class Attachment(BaseModel):
    """Immutable binary attachment for multi-part documents.

    Carries binary content (screenshots, PDFs, supplementary files) without full Document machinery.
    ``mime_type`` is a cached_property — not included in ``model_dump()`` output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Metadata keys added by Document.serialize_model() to attachments
    SERIALIZE_METADATA_KEYS: ClassVar[frozenset[str]] = frozenset({"mime_type", "size"})

    name: str
    content: bytes
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _strip_serialize_metadata(cls, data: Any) -> Any:
        """Strip metadata keys from serialize_model() output before validation."""
        if isinstance(data, dict):
            d = cast(dict[str, Any], data)
            return {k: v for k, v in d.items() if k not in cls.SERIALIZE_METADATA_KEYS}
        return data

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v: str | None) -> str:
        """Normalize None to empty string. description is always str, never None."""
        if v is None:
            return ""
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        """Reject path separators, reserved suffixes, whitespace issues."""
        if v.endswith(".description.md"):
            raise DocumentNameError(f"Attachment names cannot end with .description.md: {v}")
        if v.endswith(".sources.json"):
            raise DocumentNameError(f"Attachment names cannot end with .sources.json: {v}")
        if v.endswith(".attachments.json"):
            raise DocumentNameError(f"Attachment names cannot end with .attachments.json: {v}")
        if "\\" in v or "/" in v:
            raise DocumentNameError(f"Invalid attachment name - contains path separator: {v}")
        if not v or v.startswith(" ") or v.endswith(" "):
            raise DocumentNameError(f"Invalid attachment name format: {v}")
        return v

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, v: Any) -> bytes:
        """Convert content to bytes.

        Handles:
        1. bytes — passed through directly
        2. str with data URI prefix — base64-decoded to bytes
        3. str (plain text) — UTF-8 encoded to bytes
        """
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            # Data URIs are produced by serialize_content() for binary content only (failed UTF-8 decode).
            # Text starting with "data:<mime>;base64," would be misinterpreted, accepted by design.
            if _DATA_URI_PATTERN.match(v):
                _, payload = v.split(",", 1)
                return base64.b64decode(payload, validate=True)
            return v.encode("utf-8")
        raise ValueError(f"Invalid content type: {type(v)}")

    @field_serializer("content")
    def _serialize_content(self, v: bytes) -> str:
        """Serialize content: plain string for text, data URI (RFC 2397) for binary."""
        return _serialize_content_bytes(v, self.mime_type)

    @cached_property
    def mime_type(self) -> str:
        """Detected MIME type from content and filename. Cached."""
        return detect_mime_type(self.content, self.name)

    @property
    def is_image(self) -> bool:
        """True if MIME type starts with image/."""
        return is_image_mime_type(self.mime_type)

    @property
    def is_pdf(self) -> bool:
        """True if MIME type is application/pdf."""
        return is_pdf_mime_type(self.mime_type)

    @property
    def is_text(self) -> bool:
        """True if MIME type indicates text content."""
        return is_text_mime_type(self.mime_type)

    @property
    def size(self) -> int:
        """Content size in bytes."""
        return len(self.content)

    @property
    def text(self) -> str:
        """Content decoded as UTF-8. Raises ValueError if not text."""
        if not self.is_text:
            raise ValueError(f"Attachment is not text: {self.name}")
        return self.content.decode("utf-8")
