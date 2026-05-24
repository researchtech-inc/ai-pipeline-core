"""Local exception hierarchy for ``_llm_core``.

Defining these locally lets ``_llm_core`` stand on its own — no import from
``ai_pipeline_core.exceptions`` or any framework code. The integrating
package (``ai_pipeline_core``) re-exports them through its own
``exceptions`` module so existing callers keep working unchanged.

``NonRetriableError`` lives here too so ``TerminalError`` directly
inherits from it, and framework retry machinery's
``isinstance(exc, NonRetriableError)`` check honors LLM terminal errors
without further plumbing. ``ai_pipeline_core._base_exceptions`` imports
``NonRetriableError`` from this module.
"""

from collections.abc import Mapping
from typing import Literal

__all__ = [
    "AIPLHeaderParseError",
    "ContentPolicyError",
    "EmptyResponseError",
    "GroupExhaustedError",
    "LLMCoreError",
    "LLMError",
    "LLMValidationError",
    "MidStreamProviderError",
    "NonRetriableError",
    "OutputDegenerationError",
    "PartialToolCallStreamError",
    "PayloadTooLargeError",
    "ProseContaminationError",
    "ProviderReasoningReplayError",
    "StreamWatchdogError",
    "StrictModeViolationError",
    "StructuredRepairExhaustedError",
    "StructuredSchemaError",
    "TerminalError",
]


class LLMCoreError(Exception):
    """Root exception for ``_llm_core`` failures."""


class NonRetriableError(Exception):
    """Marker base: this exception must not trigger task/flow retry loops.

    Defined in ``_llm_core/exceptions.py`` (not ``_base_exceptions.py``) so
    ``TerminalError`` IS-A ``NonRetriableError`` without ``_llm_core``
    depending on framework code. ``ai_pipeline_core._base_exceptions``
    re-exports this class for application-facing imports.
    """


class LLMError(LLMCoreError):
    """Raised when LLM generation fails after all retries, including timeouts and provider errors."""


class TerminalError(LLMError, NonRetriableError):
    """Failure that requires an external fix and must stop framework retry loops."""


class GroupExhaustedError(LLMError):
    """The AIPL proxy exhausted one logical model group and fallback should advance."""


class ContentPolicyError(LLMError):
    """Provider blocked a response for content-policy reasons."""

    def __init__(
        self,
        message: str,
        *,
        deployment_id: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.deployment_id = deployment_id
        self.response_headers = response_headers


class LLMValidationError(LLMError):
    """Structured LLM output failed validation after framework retries.

    ``original`` is ``BaseException`` (not narrowed to ``ValidationError``)
    so subclasses can carry transport-level provider errors (e.g.
    ``openai.BadRequestError``) without losing the underlying cause.
    """

    def __init__(self, message: str, *, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.original = original


class StreamWatchdogError(LLMError):
    """Streaming response violated the configured watchdog policy.

    ``reason`` identifies which gate tripped: ``ttft`` (no first chunk within
    the time-to-first-token budget), ``stall`` (no chunk within the
    inter-chunk inactivity budget), or ``total`` (overall wall-clock budget).
    """

    def __init__(
        self,
        reason: Literal["ttft", "stall", "total"],
        deployment_id: str | None,
        observed_tps: float | None,
    ) -> None:
        super().__init__(f"Streaming response failed watchdog check ({reason}).")
        self.reason = reason
        self.deployment_id = deployment_id
        self.observed_tps = observed_tps


class OutputDegenerationError(LLMError):
    """LLM output contains degeneration patterns (e.g., token repetition loops). Triggers retry with cache disabled."""


class EmptyResponseError(LLMError):
    """Model returned empty content (no text, tool calls, or reasoning signal).

    Carries ``deployment_id`` and ``response_headers`` so ``_classify_failure``
    can append the failed deployment to the request-scoped skip list and
    prevent HRW affinity from re-pinning the same empty-returning lane.
    """

    def __init__(
        self,
        message: str,
        *,
        deployment_id: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.deployment_id = deployment_id
        self.response_headers = response_headers or {}


class AIPLHeaderParseError(LLMError):
    """AIPL proxy returned malformed control-plane headers."""


class MidStreamProviderError(LLMError):
    """Provider stream failed after at least one committed chunk arrived."""

    def __init__(
        self,
        message: str,
        *,
        original: BaseException | None = None,
        deployment_id: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.original = original
        self.deployment_id = deployment_id
        self.response_headers = response_headers or {}
        self.status_code = getattr(original, "status_code", None)
        self.response = getattr(original, "response", None)


class PartialToolCallStreamError(LLMError):
    """Provider stream ended with an incomplete tool-call delta."""

    def __init__(
        self,
        message: str,
        *,
        deployment_id: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.deployment_id = deployment_id
        self.response_headers = response_headers or {}


class PayloadTooLargeError(TerminalError):
    """Request payload exceeds a known provider/proxy inline size limit."""


class ProviderReasoningReplayError(LLMValidationError):
    """Upstream rejected a replayed reasoning signature blob.

    Carries enough context for the retry path to strip signatures and re-pin
    routing to the deployment that just rejected the blob.
    """

    def __init__(
        self,
        message: str,
        *,
        original: BaseException | None = None,
        deployment_id: str | None = None,
    ) -> None:
        super().__init__(message, original=original)
        self.deployment_id = deployment_id


class ProseContaminationError(LLMValidationError):
    """Structured output was wrapped in prose or markdown and could not be cleanly parsed."""


class StructuredSchemaError(LLMValidationError):
    """Structured output parsed as JSON but violated the declared Pydantic schema."""


class StructuredRepairExhaustedError(LLMValidationError):
    """Structured-output repair loop exhausted all attempts for one model hop.

    Distinct from ``StructuredSchemaError``: raised after the per-model retry
    budget is spent so ``_aipl`` routes it as terminal-for-model and advances
    the fallback chain instead of consuming another retry slot.
    """


class StrictModeViolationError(LLMValidationError):
    """Provider accepted a strict structured-output schema but returned keys outside it."""
