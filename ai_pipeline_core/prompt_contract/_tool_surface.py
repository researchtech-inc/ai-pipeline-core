"""Typed tool surface used by ``PromptContract.tools``.

``ToolBinding`` lives in ``ai_pipeline_core.llm._tool_binding`` (LLM layer
owns the tool-class-to-args binding type). ``prompt_contract`` re-exports it
through ``prompt_contract/__init__.py`` so the public import path is stable.
"""

from dataclasses import dataclass

from ai_pipeline_core.llm.tools import Tool

__all__ = ["ToolAvailability"]


@dataclass(frozen=True, slots=True)
class ToolAvailability:
    """Declares a tool class as available to a ``PromptContract``.

    ``max_calls`` caps the total invocations of this tool across one
    contract execution. The engine enforces the cap per tool class and
    returns a budget-exhausted error to the model for calls beyond it.
    """

    tool: type[Tool]
    max_calls: int

    def __post_init__(self) -> None:
        # Defensive runtime validation against callers that ignore the
        # static type. Pyright considers the ``tool`` branch unreachable
        # because ``tool: type[Tool]`` is the declared field type; the
        # check exists to defend against runtime callers that bypass the
        # annotation.
        if not isinstance(self.tool, type) or not issubclass(self.tool, Tool):  # type: ignore[unreachable]
            raise TypeError(f"ToolAvailability.tool must be a Tool subclass, got {self.tool!r}")
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls < 1:
            raise TypeError(f"ToolAvailability.max_calls must be a positive int, got {self.max_calls!r}")
