"""Tests for Document serialize_model / model_validate with attachments."""

import base64

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.attachment import Attachment


class SerFlowDoc(Document):
    """Concrete Document for serialization tests."""


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


# --- Binary fixtures ---

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
PDF_HEADER = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Type/Catalog>>\nendobj"


class TestSerializeTextAttachment:
    """Test serialization of text attachments."""

    def test_text_attachment_fields(self):
        att = Attachment(name="notes.txt", content=b"Hello world")
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=(att,))
        serialized = doc.serialize_model()

        assert len(serialized["attachments"]) == 1
        att_dict = serialized["attachments"][0]
        assert att_dict["name"] == "notes.txt"
        # Text content serialized as plain string
        assert att_dict["content"] == "Hello world"
        assert "text" in att_dict["mime_type"]
        assert att_dict["size"] == len(b"Hello world")


class TestSerializeBinaryAttachment:
    """Test serialization of binary attachments."""

    def test_binary_attachment_base64_encoding(self):
        att = Attachment(name="image.png", content=PNG_HEADER)
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=(att,))
        serialized = doc.serialize_model()

        att_dict = serialized["attachments"][0]
        # Binary content serialized as data URI
        expected_b64 = base64.b64encode(PNG_HEADER).decode("ascii")
        assert att_dict["content"] == f"data:{att_dict['mime_type']};base64,{expected_b64}"
        assert "image" in att_dict["mime_type"]
        assert att_dict["size"] == len(PNG_HEADER)


class TestSerializeAttachmentWithDescription:
    """Test that description field is present in serialized attachment."""

    def test_description_included(self):
        att = Attachment(name="screenshot.jpg", content=PNG_HEADER, description="Homepage capture")
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=(att,))
        serialized = doc.serialize_model()

        att_dict = serialized["attachments"][0]
        assert att_dict["description"] == "Homepage capture"

    def test_description_empty_when_absent(self):
        att = Attachment(name="file.txt", content=b"data")
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=(att,))
        serialized = doc.serialize_model()

        att_dict = serialized["attachments"][0]
        assert att_dict["description"] == ""


class TestSerializeWithoutAttachments:
    """Test serialization when no attachments are present."""

    def test_empty_attachments_list_in_output(self):
        doc = SerFlowDoc(name="report.txt", content=b"body")
        serialized = doc.serialize_model()
        assert "attachments" in serialized
        assert serialized["attachments"] == []


class TestRoundtrip:
    """Test serialize -> deserialize roundtrip preserves all fields."""

    def test_text_attachment_roundtrip(self):
        att = Attachment(name="notes.txt", content=b"Hello world", description="My notes")
        doc = SerFlowDoc(name="report.txt", content=b"body text", attachments=(att,))
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)

        assert restored.name == doc.name
        assert restored.content == doc.content
        assert len(restored.attachments) == 1
        assert restored.attachments[0].name == att.name
        assert restored.attachments[0].content == att.content
        assert restored.attachments[0].description == att.description

    def test_binary_attachment_roundtrip(self):
        att = Attachment(name="image.png", content=PNG_HEADER)
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=(att,))
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)

        assert len(restored.attachments) == 1
        assert restored.attachments[0].name == "image.png"
        assert restored.attachments[0].content == PNG_HEADER

    def test_no_attachments_roundtrip(self):
        doc = SerFlowDoc(name="report.txt", content=b"body")
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)

        assert restored.attachments == ()
        assert restored.content == doc.content

    def test_sha256_preserved_after_roundtrip(self):
        att = Attachment(name="a.txt", content=b"data")
        doc = SerFlowDoc(name="test.txt", content=b"content", attachments=(att,))
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)
        assert restored.sha256 == doc.sha256


class TestMultipleAttachmentsOrdering:
    """Test that multiple attachments preserve order through serialization."""

    def test_ordering_preserved(self):
        atts = (
            Attachment(name="first.txt", content=b"111"),
            Attachment(name="second.txt", content=b"222"),
            Attachment(name="third.txt", content=b"333"),
        )
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=atts)
        serialized = doc.serialize_model()

        assert len(serialized["attachments"]) == 3
        assert serialized["attachments"][0]["name"] == "first.txt"
        assert serialized["attachments"][1]["name"] == "second.txt"
        assert serialized["attachments"][2]["name"] == "third.txt"

    def test_ordering_preserved_through_roundtrip(self):
        atts = (
            Attachment(name="a.txt", content=b"aaa"),
            Attachment(name="b.txt", content=b"bbb"),
            Attachment(name="c.txt", content=b"ccc"),
        )
        doc = SerFlowDoc(name="report.txt", content=b"body", attachments=atts)
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)

        assert len(restored.attachments) == 3
        for i, name in enumerate(["a.txt", "b.txt", "c.txt"]):
            assert restored.attachments[i].name == name


class TestMixedAttachments:
    """Test serialization with mixed text and binary attachments."""

    def test_mixed_text_and_binary_serialized_correctly(self):
        text_att = Attachment(name="notes.txt", content=b"text content")
        binary_att = Attachment(name="image.png", content=PNG_HEADER)
        pdf_att = Attachment(name="doc.pdf", content=PDF_HEADER)
        doc = SerFlowDoc(
            name="report.txt",
            content=b"body",
            attachments=(text_att, binary_att, pdf_att),
        )
        serialized = doc.serialize_model()

        assert len(serialized["attachments"]) == 3
        # Text attachment - plain string
        assert serialized["attachments"][0]["content"] == "text content"
        # Binary (PNG) attachment - data URI
        expected_png = base64.b64encode(PNG_HEADER).decode("ascii")
        assert serialized["attachments"][1]["content"].startswith("data:image/png;base64,")
        assert serialized["attachments"][1]["content"] == f"data:image/png;base64,{expected_png}"
        # Binary (PDF) attachment - data URI
        expected_pdf = base64.b64encode(PDF_HEADER).decode("ascii")
        assert serialized["attachments"][2]["content"].startswith("data:application/pdf;base64,")
        assert serialized["attachments"][2]["content"] == f"data:application/pdf;base64,{expected_pdf}"

    def test_mixed_roundtrip(self):
        text_att = Attachment(name="notes.txt", content=b"text content", description="Notes")
        binary_att = Attachment(name="image.png", content=PNG_HEADER)
        doc = SerFlowDoc(
            name="report.txt",
            content=b"body",
            attachments=(text_att, binary_att),
        )
        serialized = doc.serialize_model()
        restored = SerFlowDoc.model_validate(serialized)

        assert len(restored.attachments) == 2
        assert restored.attachments[0].name == "notes.txt"
        assert restored.attachments[0].content == b"text content"
        assert restored.attachments[0].description == "Notes"
        assert restored.attachments[1].name == "image.png"
        assert restored.attachments[1].content == PNG_HEADER
