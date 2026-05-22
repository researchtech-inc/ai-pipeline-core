"""PromptResult container returned by ``PromptContract.execute()``."""

from dataclasses import dataclass, field

from ai_pipeline_core._pydantic_base import FrozenBaseModel

from ._cited_text import DocumentCitation

__all__ = ["PromptResult"]


@dataclass(frozen=True, slots=True)
class PromptResult[OutputT: FrozenBaseModel]:
    """Outcome of a ``PromptContract`` execution.

    ``response`` is the parsed structured output (a ``BaseModel`` instance).
    ``citations`` mirrors the document and URL citations collected while
    satisfying ``CitedText`` fields and any search-enabled model annotations.
    """

    response: OutputT
    citations: tuple[DocumentCitation, ...] = field(default_factory=tuple)
