"""Exception hierarchy for AI Pipeline Core.

Re-exports the LLM exception hierarchy from
``ai_pipeline_core._llm_core.exceptions`` and adds framework-only
exceptions (``RetryableError``, ``StubNotImplementedError``,
``PipelineCoreError``). ``_llm_core`` defines its own exception
hierarchy so it can stand alone; the framework re-exports them so
existing application import paths keep working.
"""

from ai_pipeline_core._base_exceptions import NonRetriableError, PipelineCoreError, StubNotImplementedError
from ai_pipeline_core._llm_core.exceptions import (
    AIPLHeaderParseError,
    ContentPolicyError,
    EmptyResponseError,
    GroupExhaustedError,
    LLMError,
    LLMValidationError,
    MidStreamProviderError,
    OutputDegenerationError,
    PartialToolCallStreamError,
    PayloadTooLargeError,
    ProseContaminationError,
    ProviderReasoningReplayError,
    StreamWatchdogError,
    StrictModeViolationError,
    StructuredSchemaError,
    TerminalError,
)
from ai_pipeline_core.documents.exceptions import DocumentNameError, DocumentSizeError, DocumentValidationError

__all__ = [
    "AIPLHeaderParseError",
    "ContentPolicyError",
    "DocumentNameError",
    "DocumentSizeError",
    "DocumentValidationError",
    "EmptyResponseError",
    "GroupExhaustedError",
    "LLMError",
    "LLMValidationError",
    "MidStreamProviderError",
    "NonRetriableError",
    "OutputDegenerationError",
    "PartialToolCallStreamError",
    "PayloadTooLargeError",
    "PipelineCoreError",
    "ProseContaminationError",
    "ProviderReasoningReplayError",
    "RetryableError",
    "StreamWatchdogError",
    "StrictModeViolationError",
    "StructuredSchemaError",
    "StubNotImplementedError",
    "TerminalError",
]


class RetryableError(PipelineCoreError):
    """Retry-eligible integration failure raised by tools or provider adapters."""
