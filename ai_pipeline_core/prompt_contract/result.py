"""PromptResult container returned by ``PromptContract.execute()``."""

from dataclasses import dataclass, field

from ai_pipeline_core._pydantic_base import FrozenBaseModel
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm._request_messages import AnyMessage

from .cited_text import DocumentCitation

__all__ = ["PromptResult"]


@dataclass(frozen=True, slots=True)
class ContinuationState:
    """Engine-private state needed to continue one prompt-contract judgment."""

    context: tuple[Document, ...]
    turns: tuple[AnyMessage, ...]
    source_span_id: str
    model_cache_fingerprint: tuple[str, bool, bool]


@dataclass(frozen=True, slots=True)
class PromptResult[OutputT: FrozenBaseModel]:
    """Outcome of a ``PromptContract`` execution.

    ``response`` is the parsed structured output (a ``BaseModel`` instance).
    ``citations`` mirrors the document and URL citations collected while
    satisfying ``CitedText`` fields and any search-enabled model annotations.
    """

    response: OutputT
    citations: tuple[DocumentCitation, ...] = field(default_factory=tuple)
    _continuation: ContinuationState | None = field(default=None, repr=False, compare=False)
