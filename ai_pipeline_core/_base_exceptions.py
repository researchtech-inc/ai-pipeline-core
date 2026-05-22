"""Base exception classes for AI Pipeline Core.

The framework-side ``NonRetriableError`` subclasses both ``PipelineCoreError``
and the ``_llm_core`` marker, so:

- Application code raising ``NonRetriableError`` produces an instance that
  ``deployment._helpers._classify_error`` recognizes as ``PIPELINE_ERROR``
  via the ``PipelineCoreError`` base.
- ``_llm_core.TerminalError`` (which extends the core marker only) is
  recognized as non-retriable by framework retry loops via the marker —
  retry sites import the marker and catch on it.

The marker lives in ``_llm_core/exceptions.py`` so ``_llm_core`` can run
standalone without framework dependencies.
"""

from ai_pipeline_core._llm_core.exceptions import NonRetriableError as _CoreNonRetriable

__all__ = ["NonRetriableError", "PipelineCoreError", "StubNotImplementedError"]


class PipelineCoreError(Exception):
    """Base exception for all AI Pipeline Core errors."""


class NonRetriableError(PipelineCoreError, _CoreNonRetriable):
    """Raised when an operation fails and must not be retried.

    The retry loops in ``PipelineTask`` and ``PipelineFlow`` stop on this
    exception (and on any ``_llm_core.exceptions.NonRetriableError`` subclass
    such as ``_llm_core.TerminalError``) without further retry attempts.

    Can wrap another exception to preserve the original cause::

        raise NonRetriableError("invalid API key") from original_exc
    """


class StubNotImplementedError(NonRetriableError):
    """Raised when a stub class (``_stub = True``) is executed at runtime.

    Stubs are placeholder classes with correct type signatures but no
    implementation. They pass all definition-time validation and type
    checking, but must not be executed. Subclasses ``NonRetriableError``
    to prevent retry loops from retrying the stub.

    To fix: implement the ``run()`` body and remove ``_stub = True``.
    """
