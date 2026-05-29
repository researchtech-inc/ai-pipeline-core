"""Document-specific exceptions."""

__all__ = [
    "DocumentNameError",
    "DocumentSizeError",
    "DocumentValidationError",
]


class DocumentValidationError(Exception):
    """Raised when document validation fails."""


class DocumentSizeError(DocumentValidationError):
    """Raised when document content exceeds MAX_CONTENT_SIZE limit."""


class DocumentNameError(DocumentValidationError):
    """Raised when document name contains path separators, reserved suffixes, or invalid format."""
