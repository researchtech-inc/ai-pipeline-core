# MODULE: exceptions
# CLASSES: LLMError, OutputDegenerationError, EmptyResponseError, PipelineCoreError, NonRetriableError, StubNotImplementedError
# DEPENDS: Exception
# VERSION: 0.22.2
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import LLMError, NonRetriableError, OutputDegenerationError, PipelineCoreError, StubNotImplementedError
```

## Public API

```python
class LLMError(PipelineCoreError):
    """Raised when LLM generation fails after all retries, including timeouts and provider errors."""


class OutputDegenerationError(LLMError):
    """LLM output contains degeneration patterns (e.g., token repetition loops). Triggers retry with cache disabled."""


class EmptyResponseError(LLMError):
    """Model returned empty content (no text and no tool calls). Retried with LiteLLM cache disabled."""


class PipelineCoreError(Exception):
    """Base exception for all AI Pipeline Core errors."""


class NonRetriableError(PipelineCoreError):
    """Raised when an operation fails and must not be retried.

    The retry loops in PipelineTask and PipelineFlow check for this exception
    and stop immediately without further retry attempts.

    Can wrap another exception to preserve the original cause::

        raise NonRetriableError("invalid API key") from original_exc"""


class StubNotImplementedError(NonRetriableError):
    """Raised when a stub class (``_stub = True``) is executed at runtime.

    Stubs are placeholder classes with correct type signatures but no implementation.
    They pass all definition-time validation and type checking, but must not be
    executed. Subclasses ``NonRetriableError`` to prevent retry loops from retrying
    the stub.

    To fix: implement the ``run()`` body and remove ``_stub = True``."""
```

## Examples

**Output degeneration error is llm error** (`tests/test_exceptions_ai_docs.py:34`)

```python
def test_output_degeneration_error_is_llm_error() -> None:
    """Degeneration failures are represented as an LLMError subclass."""
    assert isinstance(OutputDegenerationError("loop"), LLMError)
```


## Error Examples

**Document name error on path traversal** (`tests/test_exceptions_ai_docs.py:20`)

```python
class _TestExDoc(Document):
    """Document for exception testing."""


def test_document_name_error_on_path_traversal() -> None:
    """Path traversal in document names raises DocumentNameError."""
    with pytest.raises(DocumentNameError):
        _TestExDoc(name="../bad.txt", content=b"x")
```

**Document size error on limit exceeded** (`tests/test_exceptions_ai_docs.py:27`)

```python
class _TinyDoc(Document):
    """Document with tiny size limit."""

    MAX_CONTENT_SIZE = 4


def test_document_size_error_on_limit_exceeded() -> None:
    """Content beyond MAX_CONTENT_SIZE raises DocumentSizeError."""
    with pytest.raises(DocumentSizeError):
        _TinyDoc(name="tiny.txt", content=b"12345")
```
