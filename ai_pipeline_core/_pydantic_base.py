"""Shared Pydantic base class for frozen, strict models.

Public framework value types and prompt-contract output models inherit from
``FrozenBaseModel`` so they are immutable and reject unknown keys. Concrete
models still set their own field defaults and validators on top.
"""

from pydantic import BaseModel, ConfigDict

__all__ = ["FrozenBaseModel"]


class FrozenBaseModel(BaseModel):
    """``BaseModel`` with ``frozen=True`` and ``extra='forbid'`` by default."""

    model_config = ConfigDict(frozen=True, extra="forbid")
