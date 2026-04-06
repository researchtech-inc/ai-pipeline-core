"""Document system for AI pipeline flows.

Provides the Document base class (immutable, content-addressed), Attachment for
binary sub-documents, and utility functions for document handling.
"""

from ._context import (
    DocumentSha256,
)
from .attachment import Attachment
from .document import Document
from .utils import ensure_extension, find_document, replace_extension, sanitize_url

__all__ = [
    "Attachment",
    "Document",
    "DocumentSha256",
    "ensure_extension",
    "find_document",
    "replace_extension",
    "sanitize_url",
]
