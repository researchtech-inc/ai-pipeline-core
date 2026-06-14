"""Declared continuation lineage for PromptContract subclasses."""

from dataclasses import dataclass
from typing import Any

__all__ = ["Continues"]


@dataclass(frozen=True, slots=True)
class Continues:
    """Continuation declaration used as ``continues=...`` on a PromptContract subclass."""

    opener: type[Any]
    repeatable: bool

    @classmethod
    def once(cls, opener: type[Any]) -> Continues:
        """Declare one continuation step after ``opener``."""
        return cls(opener=opener, repeatable=False)

    @classmethod
    def repeating(cls, opener: type[Any]) -> Continues:
        """Declare a continuation step that may follow ``opener`` or itself."""
        return cls(opener=opener, repeatable=True)
