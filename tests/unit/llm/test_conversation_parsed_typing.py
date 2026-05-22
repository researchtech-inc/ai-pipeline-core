"""Validate Conversation.parsed raises before any send.

.parsed is typed T. Accessing it before any send() raises ValueError.
After send, it returns the typed value.
"""

import pytest

from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.llm.conversation import Conversation
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class ParsedCtxDoc(Document):
    """Test context document for parsed typing tests."""


class TestConversationParsedBeforeSend:
    """Verify .parsed raises ValueError on a fresh Conversation."""

    def test_parsed_raises_on_fresh_conversation(self) -> None:
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        with pytest.raises(ValueError, match="before calling send"):
            conv.parsed

    def test_content_is_empty_on_fresh_conversation(self) -> None:
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert conv.content == ""

    def test_cost_is_none_on_fresh_conversation(self) -> None:
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert conv.cost is None

    def test_citations_empty_on_fresh_conversation(self) -> None:
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert conv.citations == ()

    def test_parsed_raises_after_with_context(self) -> None:
        doc = ParsedCtxDoc.create_root(name="ctx.txt", content="context", reason="test")
        conv = Conversation(model=DEFAULT_TEST_MODEL).with_context(doc)
        with pytest.raises(ValueError, match="before calling send"):
            conv.parsed
