"""Conversation type detection helpers used by pipeline internals."""

from typing import Any

from ai_pipeline_core._llm_core.types import AIModel

__all__ = [
    "is_conversation_instance",
    "is_conversation_type",
]

_CONVERSATION_MODULE = "ai_pipeline_core.llm.conversation"
_CONVERSATION_CLASS = "Conversation"


def is_conversation_type(value: Any) -> bool:
    """Return whether ``value`` is the Conversation class or a parameterized variant."""
    return isinstance(value, type) and value.__module__ == _CONVERSATION_MODULE and value.__name__ == _CONVERSATION_CLASS


def is_conversation_instance(value: Any) -> bool:
    """Return whether ``value`` looks like a Conversation instance.

    Conversation requires an ``AIModel`` as its ``model`` field. We match the
    Conversation class by module+name (not by import) because importing
    ``llm.conversation`` from pipeline internals would create a cycle, but we
    can import ``AIModel`` directly because ``_llm_core`` is a lower layer.
    """
    value_type = type(value)
    return (
        value_type.__module__ == _CONVERSATION_MODULE
        and value_type.__name__ == _CONVERSATION_CLASS
        and isinstance(getattr(value, "model", None), AIModel)
        and hasattr(value, "context")
        and hasattr(value, "messages")
    )
