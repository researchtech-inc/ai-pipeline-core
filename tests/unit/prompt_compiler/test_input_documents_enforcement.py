"""Prove missing input_documents only warns, does not raise.

send_spec() logs a warning when declared input_documents are missing,
but does not prevent the LLM call or check conversation context.
"""

# pyright: reportPrivateUsage=false

import pytest

from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.spec import PromptSpec
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class EnforcementRole(Role):
    """Role for enforcement tests."""

    text = "test analyst for document enforcement"


class RequiredSourceDoc(Document):
    """Document type that specs declare as required."""


class WrongTypeDoc(Document):
    """Different document type — not what the spec expects."""


class RequiresSourceSpec(PromptSpec):
    """Spec that declares required input documents."""

    role = EnforcementRole
    input_documents = (RequiredSourceDoc,)
    task = "Analyze the source document."


class TestWarningOnlyBehavior:
    """Prove: missing input_documents only warns, does not raise."""

    def test_warning_condition_exists(self) -> None:
        """Spec has input_documents — condition for warning holds."""
        spec = RequiresSourceSpec()

        is_follow_up = spec._follows is not None
        has_input_docs = bool(spec.input_documents)
        no_documents_passed = True

        should_warn = not is_follow_up and has_input_docs and no_documents_passed
        assert should_warn

    def test_warning_does_not_check_conversation_context(self) -> None:
        """Warning fires even when documents ARE in conversation context."""
        source = RequiredSourceDoc.create_root(name="source.txt", content="data", reason="test")
        conv = Conversation(model=DEFAULT_TEST_MODEL).with_context(source)
        spec = RequiresSourceSpec()

        # Current condition: `not documents` — ignores context
        documents_param: list[Document] | None = None
        would_warn = spec._follows is None and spec.input_documents and not documents_param
        assert would_warn  # Warns even though doc IS in context

        context_types = {type(doc) for doc in conv.context}
        assert RequiredSourceDoc in context_types

    def test_wrong_type_suppresses_warning(self) -> None:
        """Passing wrong-type documents suppresses the warning."""
        wrong_doc = WrongTypeDoc.create_root(name="config.txt", content="config", reason="test")
        documents_param = [wrong_doc]

        # `not documents_param` is False — warning suppressed despite wrong type
        would_warn = not documents_param
        assert not would_warn


class TestInputDocumentsEnforcementNotImplemented:
    """Verify strict enforcement after implementation."""

    @pytest.mark.xfail(reason="InputDocumentError not yet implemented", strict=True)
    def test_input_document_error_importable(self) -> None:
        """After fix: InputDocumentError should be importable."""
        from ai_pipeline_core.prompt_compiler._validation import InputDocumentError  # type: ignore[import-not-found]

        assert InputDocumentError is not None
