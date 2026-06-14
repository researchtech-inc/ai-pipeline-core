"""ValidationFailure model returned by ``PromptContract.validate()``."""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ValidationFailure"]


class ValidationFailure(BaseModel):
    """A single semantic validation failure for a ``PromptContract`` response.

    Returned by ``PromptContract.validate(response)`` to drive the engine
    repair loop. The framework concatenates failures into a USER repair
    message and re-enters the engine.

    ``field`` is an optional dotted path into the output model
    (e.g. ``"items[0].score"``). ``message`` describes what failed and how
    the LLM should fix it.
    """

    model_config = ConfigDict(frozen=True)

    field: str | None = Field(
        default=None, description="Output field path triggering the failure, or None for a whole-response issue."
    )
    message: str = Field(description="Caller-facing description of what went wrong and how to repair it.")
