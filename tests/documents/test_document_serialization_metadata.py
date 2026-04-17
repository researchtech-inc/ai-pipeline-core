"""Tests for Document class-name serialization and model_validate round-trips."""

import pytest

from ai_pipeline_core.documents import Attachment, Document


class DebugSampleDocument(Document):
    """Sample document for debug field testing."""


class VeryLongNamedDebugSampleDocument(Document):
    """Sample document with long name for testing."""


class _SerMetaTaskDoc(Document):
    """Sample document for task-like class_name behavior tests."""


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


@pytest.mark.asyncio
async def test_model_dump_includes_class_name():
    """model_dump() always includes the concrete document subclass name."""
    doc = DebugSampleDocument(name="test.txt", content=b"test content")
    serialized = doc.model_dump(mode="json")

    assert "class_name" in serialized
    assert serialized["class_name"] == "DebugSampleDocument"
    assert "canonical_name" not in serialized


@pytest.mark.asyncio
async def test_model_dump_json_includes_class_name():
    """model_dump_json() carries class_name for JSON round-trips."""
    doc = DebugSampleDocument(name="test.txt", content=b"test content")

    serialized = doc.model_dump_json(indent=2)

    assert '"class_name": "DebugSampleDocument"' in serialized


@pytest.mark.asyncio
async def test_long_named_document_class_name():
    """class_name derives from the actual subclass name, even when long."""
    doc = VeryLongNamedDebugSampleDocument(name="test.txt", content=b"test")
    serialized = doc.model_dump(mode="json")

    assert serialized["class_name"] == "VeryLongNamedDebugSampleDocument"


@pytest.mark.asyncio
async def test_model_validate_roundtrip_preserves_content():
    """A dumped Document round-trips via model_validate()."""
    doc = DebugSampleDocument(name="test.txt", content=b"test content")
    serialized = doc.model_dump(mode="json")

    assert "class_name" in serialized

    restored = DebugSampleDocument.model_validate(serialized)
    assert restored.name == doc.name
    assert restored.content == doc.content
    assert restored.class_name == "DebugSampleDocument"


@pytest.mark.asyncio
async def test_different_document_types_have_correct_class_name():
    """Test class_name for different document types."""
    task_doc = _SerMetaTaskDoc(name="task.txt", content=b"task")
    task_serialized = task_doc.serialize_model()
    assert task_serialized["class_name"] == "_SerMetaTaskDoc"


@pytest.mark.asyncio
async def test_roundtrip_preserves_content():
    """serialize_model() remains an enriched wrapper around model_validate()."""
    doc = DebugSampleDocument(
        name="test.txt",
        content=b"test content",
        description="Test description",
        derived_from=("https://example.com/source1", "https://example.com/source2"),
    )

    serialized = doc.serialize_model()
    restored = DebugSampleDocument.model_validate(serialized)

    assert restored.name == doc.name
    assert restored.content == doc.content
    assert restored.description == doc.description
    assert restored.derived_from == doc.derived_from

    re_serialized = restored.serialize_model()
    assert re_serialized["class_name"] == "DebugSampleDocument"


@pytest.mark.asyncio
async def test_model_validate_rejects_wrong_class_name():
    """model_validate() rejects mismatched class_name values."""
    doc = DebugSampleDocument(name="test.txt", content=b"test content")
    serialized = doc.model_dump(mode="json")
    serialized["class_name"] = "WrongClass"

    with pytest.raises(TypeError, match="Cannot deserialize 'WrongClass' as 'DebugSampleDocument'"):
        DebugSampleDocument.model_validate(serialized)


@pytest.mark.asyncio
async def test_cross_type_casting_blocked_via_model_dump_roundtrip():
    """Plain model_dump() output still blocks cross-type deserialization."""
    doc = DebugSampleDocument(name="test.txt", content=b"content")
    serialized = doc.model_dump(mode="json")
    assert serialized["class_name"] == "DebugSampleDocument"

    with pytest.raises(TypeError, match="Cannot deserialize 'DebugSampleDocument' as 'VeryLongNamedDebugSampleDocument'"):
        VeryLongNamedDebugSampleDocument.model_validate(serialized)


@pytest.mark.asyncio
async def test_model_validate_allows_matching_class_name():
    """model_validate() accepts matching serialized payloads."""
    doc = DebugSampleDocument(name="test.txt", content=b"content")
    serialized = doc.model_dump(mode="json")
    restored = DebugSampleDocument.model_validate(serialized)
    assert restored.content == doc.content


@pytest.mark.asyncio
async def test_model_validate_allows_missing_class_name_for_valid_plain_dicts():
    """Pure JSON-like dicts without class_name still validate if structurally valid."""
    data = {"name": "test.txt", "content": "hello"}
    restored = DebugSampleDocument.model_validate(data)
    assert restored.name == "test.txt"


@pytest.mark.asyncio
async def test_model_validate_rejects_non_string_class_name():
    """class_name must be a string when present."""
    with pytest.raises(TypeError, match=r"class_name.*must be a string"):
        DebugSampleDocument.model_validate({"name": "x.txt", "content": "x", "class_name": 123})


# --- Attachment serialization metadata tests ---


JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 100
# PDF_HEADER needs invalid UTF-8 bytes to trigger base64 encoding
PDF_HEADER = b"%PDF-1.4\xff" + b"\x00" * 100  # \xff is invalid UTF-8 when standalone


@pytest.mark.asyncio
async def test_serialize_attachment_includes_mime_type_and_size():
    """Test that serialized attachments include mime_type and size keys."""
    att = Attachment(name="screenshot.jpg", content=JPEG_HEADER)
    doc = DebugSampleDocument(name="report.txt", content=b"text", attachments=(att,))
    serialized = doc.serialize_model()

    assert len(serialized["attachments"]) == 1
    att_dict = serialized["attachments"][0]
    assert "mime_type" in att_dict
    assert "size" in att_dict
    assert att_dict["mime_type"] == att.mime_type
    assert att_dict["size"] == att.size


@pytest.mark.asyncio
async def test_serialize_text_attachment_metadata():
    """Test mime_type and size for a text attachment."""
    content = b"hello world"
    att = Attachment(name="notes.txt", content=content)
    doc = DebugSampleDocument(name="report.txt", content=b"body", attachments=(att,))
    serialized = doc.serialize_model()

    att_dict = serialized["attachments"][0]
    # New format: content is a plain string for text
    assert isinstance(att_dict["content"], str)
    assert att_dict["content"] == "hello world"
    assert att_dict["mime_type"] == "text/plain"
    assert att_dict["size"] == len(content)


@pytest.mark.asyncio
async def test_serialize_pdf_attachment_metadata():
    """Test mime_type and size for a PDF attachment."""
    att = Attachment(name="doc.pdf", content=PDF_HEADER)
    doc = DebugSampleDocument(name="report.txt", content=b"body", attachments=(att,))
    serialized = doc.serialize_model()

    att_dict = serialized["attachments"][0]
    # New format: content is a data URI for binary
    assert isinstance(att_dict["content"], str)
    assert att_dict["content"].startswith("data:")
    assert att_dict["mime_type"] == "application/pdf"
    assert att_dict["size"] == len(PDF_HEADER)


@pytest.mark.asyncio
async def test_serialize_empty_attachments():
    """Test that empty attachments serialize to an empty list."""
    doc = DebugSampleDocument(name="report.txt", content=b"body")
    serialized = doc.serialize_model()
    assert serialized["attachments"] == []


@pytest.mark.asyncio
async def test_roundtrip_with_attachments_ignores_extra_fields():
    """Attachment metadata injected by serialize_model() is stripped before validation."""
    att = Attachment(name="screenshot.jpg", content=JPEG_HEADER)
    doc = DebugSampleDocument(name="report.txt", content=b"body", attachments=(att,))

    serialized = doc.serialize_model()
    assert "mime_type" in serialized["attachments"][0]
    assert "size" in serialized["attachments"][0]

    restored = DebugSampleDocument.model_validate(serialized)
    assert restored.name == doc.name
    assert restored.content == doc.content
    assert len(restored.attachments) == 1
    assert restored.attachments[0].name == att.name
    assert restored.attachments[0].content == att.content
