"""PromptContract: typed prompt execution surface."""

from ai_pipeline_core.llm._tool_binding import ToolBinding

from .cited_text import CitedText, DocumentCitation
from .continuation import Continues
from .contract import OutputT, PromptContract
from .methodology import Methodology
from .result import PromptResult
from .tool_surface import ToolAvailability
from .validation import ValidationFailure

__all__ = [
    "CitedText",
    "Continues",
    "DocumentCitation",
    "Methodology",
    "OutputT",
    "PromptContract",
    "PromptResult",
    "ToolAvailability",
    "ToolBinding",
    "ValidationFailure",
]
