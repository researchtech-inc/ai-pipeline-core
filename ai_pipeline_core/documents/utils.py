"""Utility functions for document handling.

Provides helper functions for URL sanitization, naming conventions,
hash validation, and shared constants used throughout the document system.
"""

import base64
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_pipeline_core.documents.exceptions import DocumentValidationError

__all__ = [
    "ensure_extension",
    "find_document",
    "replace_extension",
    "sanitize_url",
]

# Regex for detecting data URIs (RFC 2397): data:<mime>;base64,<payload>
_DATA_URI_PATTERN = re.compile(r"^data:[a-zA-Z0-9.+/-]+;base64,")


def _serialize_content_bytes(content: bytes, mime_type: str) -> str:  # pyright: ignore[reportUnusedFunction]  # used by document.py and attachment.py
    """Serialize bytes as UTF-8 text when possible, otherwise as a data URI."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"


def sanitize_url(url: str) -> str:
    """Sanitize URL or query string for use as a filename (max 100 chars)."""
    # Remove protocol if it's a URL
    if url.startswith(("http://", "https://")):
        parsed = urlparse(url)
        # Use domain + path
        url = parsed.netloc + parsed.path

    # Replace invalid filename characters
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", url)

    # Replace multiple underscores with single one
    sanitized = re.sub(r"_+", "_", sanitized)

    # Remove leading/trailing underscores and dots
    sanitized = sanitized.strip("_.")

    # Limit length to prevent too long filenames
    if len(sanitized) > 100:
        sanitized = sanitized[:100]

    # Ensure we have something
    if not sanitized:
        sanitized = "unnamed"

    return sanitized


_MIN_HASH_UNIQUE_CHARS = 6


def _is_document_sha256(value: str) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by document.py, _execution_context.py
    """Check if a string is a valid base32-encoded SHA256 hash (26 chars, A-Z2-7, sufficient entropy)."""
    if not isinstance(value, str) or len(value) != 26:  # pyright: ignore[reportUnnecessaryIsInstance]
        return False

    # Check if all characters are valid base32 (A-Z, 2-7)
    if not re.match(r"^[A-Z2-7]{26}$", value):
        return False

    unique_chars = len(set(value))
    return unique_chars >= _MIN_HASH_UNIQUE_CHARS


def ensure_extension(name: str, ext: str) -> str:
    """Ensure a filename has the given extension, adding it only if missing.

    Prevents double-extension bugs like 'report.md.md' from ad-hoc string concatenation.
    """
    if not ext.startswith("."):
        ext = f".{ext}"
    if name.endswith(ext):
        return name
    return name + ext


def replace_extension(name: str, ext: str) -> str:
    """Replace the file extension (or add one if missing).

    Handles compound extensions like '.tar.gz' by replacing only the last extension.
    """
    if not ext.startswith("."):
        ext = f".{ext}"
    dot_pos = name.rfind(".")
    if dot_pos > 0:
        return name[:dot_pos] + ext
    return name + ext


def find_document[T](documents: Sequence[Any], doc_type: type[T]) -> T:
    """Find a document of the given type in a sequence.

    Replaces bare `next(d for d in docs if isinstance(d, T))` which gives opaque
    StopIteration on missing types. Raises DocumentValidationError with a clear message
    listing available document types.
    """
    for doc in documents:
        if isinstance(doc, doc_type):
            return doc
    available = sorted({type(d).__name__ for d in documents})
    raise DocumentValidationError(f"No document of type '{doc_type.__name__}' found. Available types: {', '.join(available) or 'none'}")


def load_document_from_file[D](doc_type: type[D], file_path: Path, *, reason: str = "loaded from file") -> D:
    """Load a local file from disk and wrap it as a typed root Document."""
    content_bytes = file_path.read_bytes()
    try:
        content: str | bytes = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes
    return doc_type.create_root(name=file_path.name, content=content, reason=reason)  # type: ignore[return-value]
