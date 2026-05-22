"""@internal MIME type detection utilities for documents.

This module provides functions for detecting and validating MIME types
from document content and filenames. It uses a hybrid approach combining
extension-based detection for known formats and content analysis via
python-magic for unknown files.
"""

import logging

import magic

logger = logging.getLogger(__name__)


# Extension to MIME type mapping for common formats
# These are formats where extension-based detection is more reliable
EXTENSION_MIME_MAP = {
    "md": "text/markdown",
    "txt": "text/plain",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "json": "application/json",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "xml": "text/xml",
    "html": "text/html",
    "htm": "text/html",
    "py": "text/x-python",
    "css": "text/css",
    "js": "application/javascript",
    "ts": "application/typescript",
    "tsx": "application/typescript",
    "jsx": "application/javascript",
}


def detect_mime_type(content: bytes, name: str) -> str:
    """Detect MIME type via extension lookup, then python-magic content analysis.

    Never returns None or empty string. Falls back to 'application/octet-stream'.
    Extension-based detection is preferred because magic misidentifies structured text.
    """
    # Check for empty content
    if len(content) == 0:
        return "text/plain"

    # Try extension-based detection first for known formats
    # This is more reliable for text formats that magic might misidentify
    ext = name.lower().split(".")[-1] if "." in name else ""
    if ext in EXTENSION_MIME_MAP:
        return EXTENSION_MIME_MAP[ext]

    # Try content-based detection with magic
    try:
        mime = magic.from_buffer(content[:1024], mime=True)
        # If magic returns a valid mime type, use it
        if mime and mime != "application/octet-stream":
            return mime
    except (AttributeError, OSError, magic.MagicException) as e:  # fmt: skip
        logger.warning("MIME detection failed for %s: %s", name, e)
    except Exception:
        logger.exception("Unexpected error in MIME detection for %s", name)

    # Final fallback based on extension or default
    return EXTENSION_MIME_MAP.get(ext, "application/octet-stream")


_TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/yaml",
    "application/x-yaml",
)


def is_text_mime_type(mime_type: str) -> bool:
    """Check if MIME type represents text-based content (text/*, JSON, XML, YAML, JS)."""
    return mime_type.startswith(_TEXT_MIME_PREFIXES)


def is_yaml_mime_type(mime_type: str) -> bool:
    """Check if MIME type is YAML (application/yaml or application/x-yaml)."""
    return mime_type in {"application/yaml", "application/x-yaml"}


def is_pdf_mime_type(mime_type: str) -> bool:
    """Check if MIME type is application/pdf."""
    return mime_type == "application/pdf"


def is_image_mime_type(mime_type: str) -> bool:
    """Check if MIME type starts with image/."""
    return mime_type.startswith("image/")
