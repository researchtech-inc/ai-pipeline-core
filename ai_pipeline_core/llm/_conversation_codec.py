"""Codec helpers for Conversation message state."""

from typing import Any, cast

from ._request_messages import AnyMessage, AssistantMessage, ToolResultMessage, UserMessage


def serialize_message(message: AnyMessage) -> Any:
    """Encode custom conversation message wrappers for replay codec state."""
    if isinstance(message, UserMessage):
        return {"__conversation_message__": "user", "text": message.text}
    if isinstance(message, AssistantMessage):
        return {"__conversation_message__": "assistant", "text": message.text}
    if isinstance(message, ToolResultMessage):
        return {
            "__conversation_message__": "tool_result",
            "tool_call_id": message.tool_call_id,
            "function_name": message.function_name,
            "content": message.content,
        }
    return message


def deserialize_message(message: Any) -> AnyMessage:
    """Decode custom conversation message wrappers from replay codec state."""
    if not isinstance(message, dict):
        return cast(AnyMessage, message)
    kind = message.get("__conversation_message__")
    if kind == "user":
        return UserMessage(text=cast(str, message["text"]))
    if kind == "assistant":
        return AssistantMessage(text=cast(str, message["text"]))
    if kind == "tool_result":
        return ToolResultMessage(
            tool_call_id=cast(str, message["tool_call_id"]),
            function_name=cast(str, message["function_name"]),
            content=cast(str, message["content"]),
        )
    return cast(AnyMessage, message)
