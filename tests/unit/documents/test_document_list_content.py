"""Tests for Document[list[T]] support — content validation, parsing, co-location."""

import json

import pytest
from pydantic import BaseModel

from ai_pipeline_core.documents import Document


class ItemModel(BaseModel):
    name: str
    value: int


# --- Document[list[T]] definition ---


class ListDoc(Document[list[ItemModel]]):  # pyright: ignore[reportInvalidTypeArguments]
    """Document with list content type."""


def test_document_list_content_type_is_item_type() -> None:
    assert ListDoc.get_content_type() is ItemModel


def test_document_list_content_is_list_true() -> None:
    assert ListDoc.content_is_list() is True


def test_document_list_content_is_list_false_for_scalar() -> None:
    class ScalarDoc(Document[ItemModel]):
        """Document with scalar content type."""

    assert ScalarDoc.content_is_list() is False


# --- Content validation ---


def test_document_list_create_with_list_of_models() -> None:
    items = [ItemModel(name="a", value=1), ItemModel(name="b", value=2)]
    doc = ListDoc.create_root(
        name="items.json",
        content=items,
        reason="test",
    )
    assert doc.content


def test_document_list_create_with_json_bytes() -> None:
    data = [{"name": "a", "value": 1}]
    doc = ListDoc.create_root(
        name="items.json",
        content=json.dumps(data).encode(),
        reason="test",
    )
    assert doc.content


def test_document_list_rejects_wrong_item_type() -> None:
    class OtherModel(BaseModel):
        x: str

    items = [OtherModel(x="nope")]
    with pytest.raises(TypeError, match="expected ItemModel"):
        ListDoc.create_root(name="items.json", content=items, reason="test")


def test_document_list_rejects_non_structured_extension() -> None:
    items = [ItemModel(name="a", value=1)]
    with pytest.raises(ValueError):
        ListDoc.create_root(name="items.txt", content=items, reason="test")


def test_document_list_empty_list_valid() -> None:
    doc = ListDoc.create_root(name="items.json", content=[], reason="test")
    assert doc.content


# --- Parsed property ---


def test_document_list_parsed_returns_list() -> None:
    items = [ItemModel(name="a", value=1), ItemModel(name="b", value=2)]
    doc = ListDoc.create_root(name="items.json", content=items, reason="test")
    parsed = doc.parsed
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0].name == "a"
    assert parsed[1].value == 2


def test_document_list_parsed_empty_list() -> None:
    doc = ListDoc.create_root(name="items.json", content=[], reason="test")
    assert doc.parsed == []


# --- Co-location enforcement ---


def test_document_list_colocation_same_module_passes() -> None:
    # ListDoc and ItemModel are both defined in this module — should work
    assert ListDoc.get_content_type() is ItemModel


def test_document_list_rejects_list_str() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadListDoc(Document[list[str]]):  # type: ignore[type-arg]
            """Bad."""


def test_document_list_rejects_list_int() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadListDoc2(Document[list[int]]):  # type: ignore[type-arg]
            """Bad."""
