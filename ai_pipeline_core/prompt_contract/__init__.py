"""PromptContract: typed prompt execution surface."""

from ai_pipeline_core.llm._tool_binding import ToolBinding

from ._cited_text import CitedText, DocumentCitation
from ._contract import OutputT, PromptContract
from ._methodology import Methodology
from ._result import PromptResult
from ._tool_surface import ToolAvailability
from ._validation import ValidationFailure

__all__ = [
    "CitedText",
    "DocumentCitation",
    "Methodology",
    "OutputT",
    "PromptContract",
    "PromptResult",
    "ToolAvailability",
    "ToolBinding",
    "ValidationFailure",
]
