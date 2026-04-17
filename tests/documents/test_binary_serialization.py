"""Tests for binary content serialization roundtrip via serialize_model/model_validate."""

import base64

import pytest

from ai_pipeline_core.documents import Attachment

from tests.support.helpers import ConcreteDocument


# Minimal valid PNG (1x1 transparent pixel)
MINIMAL_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# Minimal valid JPEG
MINIMAL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
    "CAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAA"
    "AAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMB"
    "AAIRAxEAPwCwAB//2Q=="
)

# Some arbitrary binary data (not valid image)
BINARY_DATA = bytes(range(256))


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestDocumentModelValidate:
    """model_validate deserializes Documents and enforces class-name boundaries."""

    def test_model_validate_roundtrips_standard_dump(self):
        original = ConcreteDocument.create_root(name="test.txt", content="hello", reason="test")
        dumped = original.model_dump(mode="json")
        restored = ConcreteDocument.model_validate(dumped)

        assert restored.sha256 == original.sha256

    def test_cross_type_cast_raises_type_error(self):
        original = ConcreteDocument.create_root(name="test.txt", content="hello", reason="test")
        dumped = original.model_dump(mode="json")
        dumped["class_name"] = "WrongDocumentType"
        with pytest.raises(TypeError, match=r"Cannot deserialize 'WrongDocumentType' as 'ConcreteDocument'"):
            ConcreteDocument.model_validate(dumped)

    def test_model_validate_allows_valid_dict_without_class_name(self):
        restored = ConcreteDocument.model_validate({"name": "test.txt", "content": "hello"})
        assert restored.content == b"hello"


class TestDocumentSerializationRoundtrip:
    """Tests proving Document content roundtrips correctly via serialize_model/model_validate."""

    def test_text_content_roundtrip(self):
        original = ConcreteDocument.create_root(
            name="test.txt",
            content="Hello, World! 你好世界 🎉",
            reason="test input",
        )
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256
        assert restored.content == original.content

    def test_binary_content_roundtrip(self):
        original = ConcreteDocument(name="image.png", content=MINIMAL_PNG)
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256
        assert restored.content == original.content

    def test_jpeg_content_roundtrip(self):
        original = ConcreteDocument(name="photo.jpg", content=MINIMAL_JPEG)
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256

    def test_arbitrary_binary_roundtrip(self):
        original = ConcreteDocument(name="data.bin", content=BINARY_DATA)
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256

    def test_serialize_model_roundtrip_works(self):
        original = ConcreteDocument(name="image.png", content=MINIMAL_PNG)
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256
        assert restored.content == original.content


class TestAttachmentPydanticSerializationBug:
    """Tests proving Attachment binary content is corrupted via Pydantic path."""

    def test_text_attachment_pydantic_roundtrip_works(self):
        """Text attachment should roundtrip correctly via Pydantic (sanity check)."""
        original = Attachment(
            name="notes.txt",
            content=b"Hello, World!",
        )
        original_content = original.content

        dumped = original.model_dump(mode="json")
        restored = Attachment.model_validate(dumped)

        assert restored.content == original_content, "Text attachment should roundtrip"

    def test_binary_attachment_pydantic_roundtrip_corrupted(self):
        """PROVES BUG: Binary attachment is CORRUPTED via Pydantic path.

        This test should FAIL before the fix.
        After fix, this test should PASS.
        """
        original = Attachment(
            name="screenshot.png",
            content=MINIMAL_PNG,
        )
        original_content = original.content

        dumped = original.model_dump(mode="json")
        restored = Attachment.model_validate(dumped)

        assert restored.content == original_content, (
            f"Binary attachment content mismatch!\n"
            f"Original length: {len(original_content)}\n"
            f"Restored length: {len(restored.content)}\n"
            f"This proves the Pydantic serialization bug exists."
        )

    def test_jpeg_attachment_pydantic_roundtrip_corrupted(self):
        """PROVES BUG: JPEG attachment is CORRUPTED via Pydantic path."""
        original = Attachment(
            name="photo.jpg",
            content=MINIMAL_JPEG,
        )
        original_content = original.content

        dumped = original.model_dump(mode="json")
        restored = Attachment.model_validate(dumped)

        assert restored.content == original_content, "JPEG attachment should roundtrip"


class TestDocumentWithBinaryAttachmentsRoundtrip:
    """Tests proving Document with binary attachments roundtrips via serialize_model/model_validate."""

    def test_document_with_binary_attachment_roundtrip(self):
        original = ConcreteDocument.create_root(
            name="report.md",
            content="# Report\n\nSee attached screenshot.",
            attachments=(Attachment(name="screenshot.png", content=MINIMAL_PNG),),
            reason="test input",
        )
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256
        assert restored.attachments[0].content == original.attachments[0].content

    def test_document_with_multiple_binary_attachments_roundtrip(self):
        original = ConcreteDocument.create_root(
            name="gallery.md",
            content="# Gallery",
            attachments=(
                Attachment(name="img1.png", content=MINIMAL_PNG),
                Attachment(name="img2.jpg", content=MINIMAL_JPEG),
                Attachment(name="data.bin", content=BINARY_DATA),
            ),
            reason="test input",
        )
        serialized = original.serialize_model()
        restored = ConcreteDocument.model_validate(serialized)

        assert restored.sha256 == original.sha256
        for i, (orig_att, rest_att) in enumerate(zip(original.attachments, restored.attachments, strict=True)):
            assert rest_att.content == orig_att.content, f"Attachment {i} content mismatch"


class TestSerializationFormat:
    """Verify serialize_model uses data URI format for binary content."""

    def test_serialization_output_format(self):
        doc = ConcreteDocument(name="image.png", content=MINIMAL_PNG)

        serialize_dump = doc.serialize_model()
        serialize_content = serialize_dump["content"]

        assert isinstance(serialize_content, str)
        assert serialize_content.startswith("data:image/png;base64,")
        assert "sha256" in serialize_dump
        assert "class_name" in serialize_dump

    def test_model_validate_restores_binary_correctly(self):
        doc = ConcreteDocument(name="image.png", content=MINIMAL_PNG)

        serialize_dump = doc.serialize_model()
        restored = ConcreteDocument.model_validate(serialize_dump)

        assert restored.content == MINIMAL_PNG
        assert restored.sha256 == doc.sha256
