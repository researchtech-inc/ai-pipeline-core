"""Tests proving bugs in list output implementation.

Each test targets a specific bug found in code review.
Tests should PASS after fixes are applied.
"""

from typing import Any

from pydantic import BaseModel

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage


class TaskItem(BaseModel):
    name: str
    priority: int


# --- Bug #4: _content_is_list not reset in non-list branch ---


def test_content_is_list_reset_for_scalar_subclass() -> None:
    """A scalar Document subclass should have _content_is_list=False even if defined after a list one."""
    from ai_pipeline_core.documents import Document

    class ListModel(BaseModel):
        x: int

    class ScalarModel(BaseModel):
        y: str

    class MyListDoc(Document[list[ListModel]]):  # pyright: ignore[reportInvalidTypeArguments]
        """List doc."""

    class MyScalarDoc(Document[ScalarModel]):
        """Scalar doc."""

    assert MyListDoc._content_is_list is True
    assert MyScalarDoc._content_is_list is False, "_content_is_list should be False for scalar Document"


# --- Bug #6: serialize_parsed first-item-only check ---


def test_serialize_parsed_empty_list() -> None:
    """Empty list[BaseModel] should serialize to [] not fall through."""
    response = ModelResponse[Any](
        content="[]",
        parsed=[],
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="test",
    )
    dumped = response.model_dump(mode="json")
    assert dumped["parsed"] == []


def test_serialize_parsed_list_of_models() -> None:
    """list[BaseModel] should serialize each item."""
    items = [TaskItem(name="a", priority=1), TaskItem(name="b", priority=2)]
    response = ModelResponse[Any](
        content="[]",
        parsed=items,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="test",
    )
    dumped = response.model_dump(mode="json")
    assert isinstance(dumped["parsed"], list)
    assert dumped["parsed"][0] == {"name": "a", "priority": 1}
    assert dumped["parsed"][1] == {"name": "b", "priority": 2}
