"""AI-doc examples for framework exception types."""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import DocumentNameError, DocumentSizeError, LLMError, OutputDegenerationError


class _TestExDoc(Document):
    """Document for exception testing."""


class _TinyDoc(Document):
    """Document with tiny size limit."""

    MAX_CONTENT_SIZE = 4


@pytest.mark.ai_docs
def test_document_name_error_on_path_traversal() -> None:
    """Path traversal in document names raises DocumentNameError."""
    with pytest.raises(DocumentNameError):
        _TestExDoc(name="../bad.txt", content=b"x")


@pytest.mark.ai_docs
def test_document_size_error_on_limit_exceeded() -> None:
    """Content beyond MAX_CONTENT_SIZE raises DocumentSizeError."""
    with pytest.raises(DocumentSizeError):
        _TinyDoc(name="tiny.txt", content=b"12345")


@pytest.mark.ai_docs
def test_output_degeneration_error_is_llm_error() -> None:
    """Degeneration failures are represented as an LLMError subclass."""
    assert isinstance(OutputDegenerationError("loop"), LLMError)
