"""LLM regression tests.

Covers boolean truthiness, substitutor persistence, XML injection, attachment placement,
slots, and serialization.
"""

import inspect
from typing import Any

import pytest

from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.types import ImagePreset, ModelOptions, TextContent, TokenUsage
from ai_pipeline_core.documents import Attachment
from ai_pipeline_core.llm import AIModel
from ai_pipeline_core.llm._conversation_messages import (
    _document_to_content_parts,
    _escape_xml_content,
    _escape_xml_metadata,
)
from ai_pipeline_core.llm._conversation_runtime import generation_overrides
from ai_pipeline_core.llm._substitutor import URLSubstitutor
from ai_pipeline_core.llm.conversation import Conversation

from tests.support.helpers import ConcreteDocument, TransportSpy
from tests.support.model_catalog import DEFAULT_TEST_MODEL


# =============================================================================
# Issue 1: Boolean Truthiness Bug - FIXED
# File: model_options.py:174,177,180
# Fix: Changed `if self.temperature:` to `if self.temperature is not None:`
# =============================================================================


class TestBooleanTruthinessBugFixed:
    """Tests verifying the boolean truthiness bug is FIXED."""

    def test_temperature_zero_is_included(self):
        """temperature=0.0 should be preserved in generation overrides."""
        options = ModelOptions(temperature=0.0)
        spec = generation_overrides(options)

        assert spec is not None
        assert spec.temperature == pytest.approx(0.0)

    def test_stop_empty_list_is_included(self):
        """stop=() should be preserved in generation overrides."""
        options = ModelOptions(stop=())
        spec = generation_overrides(options)

        assert spec is not None
        assert spec.stop == ()

    def test_positive_temperature_included(self):
        """Positive temperature should work (sanity check)."""
        options = ModelOptions(temperature=0.7)
        spec = generation_overrides(options)
        assert spec is not None
        assert spec.temperature == pytest.approx(0.7)


# =============================================================================
# Issue 2: Substitutor State Persistence - FIXED
# Fix: Using Field(exclude=True) instead of PrivateAttr, passed through constructor
# =============================================================================


class TestSubstitutorStatePersistenceFixed:
    """Tests verifying enable_substitutor flag persists across send() calls."""

    @pytest.mark.asyncio
    async def test_enable_substitutor_preserved_through_send(
        self,
        monkeypatch: pytest.MonkeyPatch,
        default_test_model: AIModel,
        transport_spy: TransportSpy,
    ):
        """enable_substitutor flag should be preserved through send() calls."""
        _ = transport_spy
        mock_response = ModelResponse[str](
            content="Response",
            parsed="Response",
            reasoning_content="",
            citations=(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost=None,
            model="gpt-5.4-mini",
            response_id="test-id",
            thinking_blocks=None,
            provider_specific_fields=None,
        )

        async def fake_generate(*args: Any, **kwargs: Any) -> ModelResponse[str]:
            return mock_response

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        # Create conversation with enable_substitutor=True (default)
        conv1 = Conversation(model=default_test_model)
        assert conv1.enable_substitutor is True

        # Send and get new conversation
        conv2 = await conv1.send("Follow up")

        # Verify enable_substitutor is preserved
        assert conv2.enable_substitutor is True

        # Also verify explicitly disabled substitutor is preserved
        conv3 = Conversation(model=default_test_model, enable_substitutor=False)
        conv4 = await conv3.send("Follow up")
        assert conv4.enable_substitutor is False

    def test_enable_substitutor_is_regular_field(self):
        """enable_substitutor should be a regular model field."""
        assert "enable_substitutor" in Conversation.model_fields, "enable_substitutor should be a model field"


# =============================================================================
# Issue 3: Async Without Await - FIXED
# Fix: Removed `async` from prepare() - it's now a regular sync method
# =============================================================================


class TestPrepareMethodFixed:
    """Tests verifying prepare() is now synchronous."""

    def test_prepare_method_is_not_async(self):
        """prepare() should be a regular sync method, not async."""
        # After fix, prepare() should NOT be a coroutine function
        assert not inspect.iscoroutinefunction(URLSubstitutor.prepare), "prepare() should be sync, not async"

    def test_prepare_can_be_called_synchronously(self):
        """prepare() should be callable without await."""
        sub = URLSubstitutor()
        # This should work without await
        sub.prepare(["https://example.com/test"])
        assert sub.is_prepared


# =============================================================================
# Issue 4: XML Injection Vulnerability - FIXED
# Fix: _escape_xml_content escapes only wrapper tag names (document, content, etc.)
#      _escape_xml_metadata escapes XML-sensitive metadata for text/attribute positions
#      Quotes, ampersands, and non-wrapper tags are never escaped.
# =============================================================================


class TestXmlInjectionFixed:
    """Tests verifying XML injection is prevented without over-escaping."""

    def test_escape_xml_content_escapes_wrapper_tags(self):
        """Wrapper tags (document, content, attachment) must be escaped in content."""
        assert "&lt;/content&gt;" in _escape_xml_content("break </content> out")
        assert "&lt;document&gt;" in _escape_xml_content("<document>")
        assert "&lt;/attachment&gt;" in _escape_xml_content("</attachment>")

    def test_escape_xml_content_preserves_non_wrapper_tags(self):
        """Non-wrapper HTML/XML tags pass through untouched."""
        assert _escape_xml_content("<div>hello</div>") == "<div>hello</div>"
        assert _escape_xml_content("<span class='x'>") == "<span class='x'>"

    def test_escape_xml_content_preserves_json(self):
        """JSON content passes through completely unescaped."""
        json = '{"key": "value", "url": "https://x.com?a=1&b=2"}'
        assert _escape_xml_content(json) == json

    def test_escape_xml_metadata_escapes_angle_brackets(self):
        """Metadata escaping protects XML-sensitive characters."""
        assert _escape_xml_metadata("<test>") == "&lt;test&gt;"
        assert _escape_xml_metadata("a & b") == "a &amp; b"
        assert _escape_xml_metadata('"quoted"') == "&quot;quoted&quot;"

    def test_document_content_wrapper_tags_escaped(self):
        """Wrapper tags in document content are escaped, other content preserved."""
        doc = ConcreteDocument.create_root(
            name="test.txt", content="Hello </content> & friends <world>", reason="test input"
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)
        combined = "".join(p.text for p in parts if isinstance(p, TextContent))

        assert "&lt;/content&gt;" in combined, "Wrapper tag must be escaped"
        assert "& friends" in combined, "Ampersand must NOT be escaped"
        assert "<world>" in combined, "Non-wrapper tag must NOT be escaped"

    def test_document_description_is_escaped(self):
        """Document description should have < and > escaped."""
        doc = ConcreteDocument.create_root(
            name="test.txt", content="Hello", description="A <test> description", reason="test input"
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)
        combined = "".join(p.text for p in parts if isinstance(p, TextContent))

        assert "&lt;test&gt;" in combined, "Description < > should be escaped"


# =============================================================================
# Issue 5: Attachments Inside Document Wrapper - FIXED
# Fix: Moved attachment processing inside document wrapper
# =============================================================================


class TestAttachmentsInsideWrapperFixed:
    """Tests verifying attachments are inside document wrapper."""

    def test_text_attachment_inside_document(self):
        """Text attachments should be inside the <document>...</document> wrapper."""
        doc = ConcreteDocument.create_root(
            name="main.txt",
            content="Main content",
            attachments=(Attachment(name="extra.txt", content=b"Attachment content"),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)
        combined = "".join(p.text for p in parts if isinstance(p, TextContent))

        # Find positions
        doc_close_pos = combined.find("</document>")
        attachment_pos = combined.find("extra.txt")

        assert attachment_pos != -1, "Attachment should be present"
        assert doc_close_pos != -1, "Document close tag should be present"
        assert attachment_pos < doc_close_pos, (
            f"Attachment at {attachment_pos} should be before </document> at {doc_close_pos}"
        )


# =============================================================================
# Issue 6: Backward Compatibility Shim - FIXED
# Fix: Removed token_usage property from ModelResponse
# =============================================================================


class TestBackwardCompatRemoved:
    """Tests verifying backward compatibility shims are removed."""

    def test_token_usage_property_removed(self):
        """ModelResponse should not have token_usage property."""
        assert not hasattr(ModelResponse, "token_usage"), "token_usage property should be removed"

    def test_usage_property_works(self):
        """The .usage property should work directly."""
        response = ModelResponse[str](
            content="test",
            parsed="test",
            reasoning_content="",
            citations=(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost=None,
            model="test",
            response_id="",
        )
        assert response.usage.total_tokens == 15


# =============================================================================
# Issue 7: Blocking I/O in Async Context - FIXED
# Fix: Wrapped to_core_messages() with asyncio.to_thread() in send()/send_structured()
# =============================================================================


class TestBlockingIOFixed:
    """Tests verifying blocking I/O is handled via asyncio.to_thread()."""

    def testto_core_messages_wrapped_in_to_thread(self):
        """to_core_messages() should be called via asyncio.to_thread() in the shared helper."""
        import ai_pipeline_core.llm._conversation_runtime as runtime_module

        source = inspect.getsource(runtime_module.assemble_api_messages)

        # Verify asyncio.to_thread is used with to_core_messages inside the helper
        assert "asyncio.to_thread(" in source and "to_core_messages" in source, (
            "to_core_messages should be wrapped with asyncio.to_thread in assemble_api_messages"
        )

    def test_asyncio_imported(self):
        """asyncio module should be imported in the runtime helper."""
        import ai_pipeline_core.llm._conversation_runtime as runtime_module

        assert hasattr(runtime_module, "asyncio") or "import asyncio" in inspect.getsource(runtime_module)


# =============================================================================
# Issue 8: Missing slots=True on Dataclasses - FIXED
# Fix: Added slots=True to Citation and URLSubstitutor
# =============================================================================


class TestSlotsAddedFixed:
    """Tests verifying slots=True was added to dataclasses."""

    def test_citation_has_slots(self):
        """Citation dataclass should have __slots__."""
        assert hasattr(Citation, "__slots__"), "Citation should have slots=True"

    def test_urlsubstitutor_has_slots(self):
        """URLSubstitutor dataclass should have __slots__."""
        assert hasattr(URLSubstitutor, "__slots__"), "URLSubstitutor should have slots=True"


# =============================================================================
# Issue 9: Magic Number Inconsistency - FIXED
# Fix: Changed 1000 to _TOKENS_PER_IMAGE = 1080
# =============================================================================


class TestMagicNumberFixed:
    """Tests verifying magic number is fixed to 1080."""

    def test_image_token_count_is_1080(self):
        """Image token estimation should use 1080 tokens per CLAUDE.md."""
        import base64

        # Create minimal valid PNG
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )

        doc = ConcreteDocument.create_root(name="test.png", content=png_data, reason="test input")
        conv = Conversation(model=DEFAULT_TEST_MODEL, context=(doc,))

        # The approximate token count should use 1080 per image
        token_count = conv.approximate_tokens_count
        assert token_count == 1080, f"Image should be 1080 tokens per CLAUDE.md, got {token_count}"


# =============================================================================
# Issue 10: Backward Compat in Substitutor - FIXED
# Fix: Removed PatternType enum and PATTERNS dict
# =============================================================================


class TestSubstitutorBackwardCompatRemoved:
    """Tests verifying backward compatibility code was removed from substitutor."""

    def test_no_pattern_type_enum(self):
        """PatternType enum should be removed."""
        import ai_pipeline_core.llm._substitutor as sub_module

        assert not hasattr(sub_module, "PatternType"), "PatternType enum should be removed"

    def test_no_patterns_dict(self):
        """PATTERNS backward compat dict should be removed."""
        import ai_pipeline_core.llm._substitutor as sub_module

        assert not hasattr(sub_module, "PATTERNS"), "PATTERNS dict should be removed"


# =============================================================================
# Issue 10: PydanticSerializationUnexpectedValue on Conversation.messages
#
# ModelResponse (bare) vs ModelResponse[Any] (parameterized) are different
# runtime classes in Pydantic v2. The _AnyMessage union expects ModelResponse[Any],
# but client.py creates bare ModelResponse. model_copy() bypasses validation,
# so bare ModelResponse lands in messages. On model_dump(), Pydantic's union
# serializer fails to match any member and emits 4 warnings per message.
# =============================================================================


class TestModelResponseSerializationWarning:
    """Bare ModelResponse in Conversation.messages must not emit serialization warnings."""

    def test_parameterized_model_response_serializes_without_warnings(self):
        """ModelResponse[Any](...) in Conversation.messages must serialize without warnings.

        The fix: client.py must create ModelResponse[Any](...) — not bare ModelResponse(...).
        ModelResponse[Any] matches the _AnyMessage union member, so Pydantic's union
        serializer recognizes it immediately without fallback warnings.
        """
        import warnings

        # Create ModelResponse[Any] — the FIXED code path (client.py:326)
        response = ModelResponse[Any](
            content="test",
            parsed="test",
            reasoning_content="",
            citations=(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost=None,
            model="test-model",
            response_id="test-id",
        )

        # Insert into Conversation via model_copy (same path as send())
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        conv_with_msg = conv.model_copy(update={"messages": (response,)})

        # model_dump() must NOT emit any warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            conv_with_msg.model_dump(mode="json")

        pydantic_warnings = [w for w in caught if "PydanticSerializationUnexpectedValue" in str(w.message)]
        assert not pydantic_warnings, (
            f"model_dump() emitted {len(pydantic_warnings)} PydanticSerializationUnexpectedValue warnings. "
            f"ModelResponse[Any] should be recognized by the union serializer."
        )

    def test_bare_model_response_is_different_class(self):
        """Verify bare ModelResponse and ModelResponse[Any] are different runtime classes.

        This is the root cause: Pydantic v2 treats GenericModel and GenericModel[Any]
        as distinct classes. If code creates bare ModelResponse(...), it won't match
        a union member typed as ModelResponse[Any].
        """
        assert ModelResponse is not ModelResponse[Any], (
            "ModelResponse and ModelResponse[Any] must be different classes — "
            "this is the Pydantic v2 generic behavior that causes the bug"
        )
