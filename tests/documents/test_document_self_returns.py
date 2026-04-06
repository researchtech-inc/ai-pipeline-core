"""Verify Document factory methods return the concrete subclass type.

Factory methods use -> Self. These tests guard the runtime behavior: every
factory method must return the exact subclass, not the base Document.
"""

from pydantic import BaseModel

from ai_pipeline_core.documents.document import Document


class SelfReturnModel(BaseModel, frozen=True):
    """Content model for typed document tests."""

    value: int


class PlainSelfDoc(Document):
    """Untyped document for Self return testing."""


class TypedSelfDoc(Document[SelfReturnModel]):
    """Typed document for Self return testing."""


class TestCreateRootReturnsSelf:
    """create_root() returns the concrete subclass at runtime."""

    def test_plain_document(self) -> None:
        doc = PlainSelfDoc.create_root(name="test.txt", content="hello", reason="test")
        assert type(doc) is PlainSelfDoc

    def test_typed_document(self) -> None:
        doc = TypedSelfDoc.create_root(name="typed.json", content=SelfReturnModel(value=42), reason="test")
        assert type(doc) is TypedSelfDoc
        assert doc.parsed.value == 42


class TestDeriveReturnsSelf:
    """derive() returns the concrete subclass at runtime."""

    def test_returns_subclass(self) -> None:
        source = PlainSelfDoc.create_root(name="source.txt", content="source", reason="test")
        derived = PlainSelfDoc.derive(name="derived.txt", content="derived", derived_from=(source,))
        assert type(derived) is PlainSelfDoc

    def test_typed_document_returns_typed_subclass(self) -> None:
        source = TypedSelfDoc.create_root(name="source.json", content=SelfReturnModel(value=1), reason="test")
        derived = TypedSelfDoc.derive(name="child.json", content=SelfReturnModel(value=2), derived_from=(source,))
        assert type(derived) is TypedSelfDoc


class TestCreateReturnsSelf:
    """create() returns the concrete subclass at runtime."""

    def test_returns_subclass(self) -> None:
        trigger = PlainSelfDoc.create_root(name="trigger.txt", content="trigger", reason="test")
        created = PlainSelfDoc.create(name="created.txt", content="result", triggered_by=(trigger,))
        assert type(created) is PlainSelfDoc


class TestCreateExternalReturnsSelf:
    """create_external() returns the concrete subclass at runtime."""

    def test_returns_subclass(self) -> None:
        trigger = PlainSelfDoc.create_root(name="plan.md", content="plan", reason="test")
        doc = PlainSelfDoc.create_external(
            name="external.txt",
            content="fetched",
            from_sources=["https://example.com/data"],
            triggered_by=(trigger,),
        )
        assert type(doc) is PlainSelfDoc
