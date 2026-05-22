"""Regression tests for document-to-content-parts conversion.

Bug 1: Image/PDF attachments on text documents are silently dropped because
_build_attachment_content returns None for non-text attachments, and the is_text
branch never calls _build_attachment_parts.

Bug 2: _process_image_to_parts passes raw bytes to ImageContent(data=...) but
the Base64Bytes field expects base64-encoded input.
"""

import base64
from io import BytesIO

from PIL import Image
from pypdf import PdfWriter

from ai_pipeline_core._llm_core.types import ImageContent, ImagePreset, PDFContent, TextContent
from ai_pipeline_core.documents import Attachment
from ai_pipeline_core.llm._conversation_messages import _document_to_content_parts

from tests.support.helpers import ConcreteDocument


def _make_image_bytes(width: int = 100, height: int = 100, fmt: str = "JPEG") -> bytes:
    """Create valid image bytes of the given size and format."""
    buf = BytesIO()
    Image.new("RGB", (width, height), (128, 128, 128)).save(buf, format=fmt)
    return buf.getvalue()


def _make_pdf_bytes(pages: int = 1) -> bytes:
    """Create a valid minimal PDF with the given number of blank pages."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Bug 1: Text documents silently drop image/PDF attachments
# ---------------------------------------------------------------------------


class TestTextDocWithImageAttachment:
    """Image attachments on text documents must be sent to the LLM."""

    def test_single_image_attachment_produces_image_content(self):
        """A text doc with one image attachment must produce ImageContent parts."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="# Website\n\nSome content here.",
            attachments=(Attachment(name="screenshot.jpg", content=_make_image_bytes()),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 1, (
            f"Expected ImageContent for screenshot attachment, got part types: {[type(p).__name__ for p in parts]}"
        )

    def test_text_content_preserved_alongside_image(self):
        """Document text must still appear when image attachment is present."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="# Website\n\nUnique marker text.",
            attachments=(Attachment(name="screenshot.jpg", content=_make_image_bytes()),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert "Unique marker text." in combined_text
        assert "<document>" in combined_text
        assert "</document>" in combined_text

    def test_image_attachment_wrapped_in_xml(self):
        """Image attachment must have <attachment> XML wrapper in text parts."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Hello world",
            attachments=(Attachment(name="screen.jpg", content=_make_image_bytes()),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert '<attachment name="screen.jpg">' in combined_text
        assert "</attachment>" in combined_text

    def test_multiple_image_attachments(self):
        """Multiple image attachments should all produce ImageContent parts."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Website with multiple screenshots.",
            attachments=(
                Attachment(name="header.jpg", content=_make_image_bytes()),
                Attachment(name="footer.png", content=_make_image_bytes(fmt="PNG")),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 2, f"Expected 2+ ImageContent parts, got {len(image_parts)}"

    def test_mixed_text_and_image_attachments(self):
        """Text attachments inline, image attachments as ImageContent."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="# Website content",
            attachments=(
                Attachment(name="notes.txt", content=b"Some text notes"),
                Attachment(name="screenshot.jpg", content=_make_image_bytes()),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        image_parts = [p for p in parts if isinstance(p, ImageContent)]

        assert "Some text notes" in combined_text, "Text attachment should be inlined"
        assert len(image_parts) >= 1, "Image attachment should produce ImageContent"

    def test_image_attachment_data_is_valid(self):
        """ImageContent data must be decodeable back to a valid image."""
        original = _make_image_bytes(200, 150)
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Website content",
            attachments=(Attachment(name="screenshot.jpg", content=original),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 1

        # Verify the stored data is a valid image
        img = Image.open(BytesIO(image_parts[0].data))
        assert img.size[0] > 0 and img.size[1] > 0

    def test_image_attachment_with_description(self):
        """Image attachment description must appear in XML wrapper."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Website content",
            attachments=(
                Attachment(name="screenshot.jpg", content=_make_image_bytes(), description="Homepage screenshot"),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert 'description="Homepage screenshot"' in combined_text

    def test_large_image_attachment_gets_split(self):
        """Image attachment exceeding model limits must be split into tiles."""
        tall_image = _make_image_bytes(1000, 5000)
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Website with tall screenshot.",
            attachments=(Attachment(name="full_page.jpg", content=tall_image),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 2, f"Expected 2+ image tiles for tall attachment, got {len(image_parts)}"

        # Each tile must be a valid image
        for part in image_parts:
            img = Image.open(BytesIO(part.data))
            assert img.size[0] > 0 and img.size[1] > 0


class TestTextDocWithPdfAttachment:
    """PDF attachments on text documents must be sent to the LLM."""

    def test_pdf_attachment_produces_pdf_content(self):
        """A text doc with a PDF attachment must produce PDFContent parts."""
        doc = ConcreteDocument.create_root(
            name="summary.md",
            content="# Summary\n\nSee attached PDF.",
            attachments=(Attachment(name="report.pdf", content=_make_pdf_bytes()),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        pdf_parts = [p for p in parts if isinstance(p, PDFContent)]
        assert len(pdf_parts) >= 1, (
            f"Expected PDFContent for PDF attachment, got part types: {[type(p).__name__ for p in parts]}"
        )

    def test_pdf_attachment_data_preserved(self):
        """PDF attachment data must match original bytes."""
        pdf_data = _make_pdf_bytes()
        doc = ConcreteDocument.create_root(
            name="summary.md",
            content="See attached.",
            attachments=(Attachment(name="report.pdf", content=pdf_data),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        pdf_parts = [p for p in parts if isinstance(p, PDFContent)]
        assert len(pdf_parts) == 1
        assert pdf_parts[0].data == pdf_data


class TestTextDocWithAllAttachmentTypes:
    """Text doc with text + image + PDF attachments simultaneously."""

    def test_all_attachment_types_present(self):
        """Text, image, and PDF attachments must all be represented."""
        doc = ConcreteDocument.create_root(
            name="report.md",
            content="# Full Report",
            attachments=(
                Attachment(name="notes.txt", content=b"Text notes"),
                Attachment(name="chart.jpg", content=_make_image_bytes()),
                Attachment(name="appendix.pdf", content=_make_pdf_bytes()),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        pdf_parts = [p for p in parts if isinstance(p, PDFContent)]

        assert "Text notes" in combined_text, "Text attachment missing"
        assert len(image_parts) >= 1, "Image attachment missing"
        assert len(pdf_parts) >= 1, "PDF attachment missing"

    def test_document_xml_wrapper_intact(self):
        """<document> wrapper must be present even with mixed attachments."""
        doc = ConcreteDocument.create_root(
            name="report.md",
            content="Report body",
            attachments=(
                Attachment(name="chart.jpg", content=_make_image_bytes()),
                Attachment(name="data.pdf", content=_make_pdf_bytes()),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert "<document>" in combined_text
        assert "</document>" in combined_text
        assert "Report body" in combined_text


# ---------------------------------------------------------------------------
# Bug 2: _process_image_to_parts passes raw bytes to Base64Bytes field
# ---------------------------------------------------------------------------


class TestImageDocumentContentParts:
    """Image documents must produce valid ImageContent via _document_to_content_parts."""

    def test_small_image_document(self):
        """Small image (no splitting needed) must produce ImageContent."""
        doc = ConcreteDocument.create_root(name="photo.jpg", content=_make_image_bytes(100, 100), reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) == 1, (
            f"Expected 1 ImageContent, got {len(image_parts)}. Types: {[type(p).__name__ for p in parts]}"
        )

    def test_small_image_data_roundtrips(self):
        """ImageContent.data must decode back to a valid image."""
        doc = ConcreteDocument.create_root(name="photo.jpg", content=_make_image_bytes(200, 150), reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 1
        img = Image.open(BytesIO(image_parts[0].data))
        assert img.size == (200, 150)

    def test_small_png_document(self):
        """PNG image must produce ImageContent with correct mime_type."""
        doc = ConcreteDocument.create_root(
            name="icon.png", content=_make_image_bytes(100, 100, fmt="PNG"), reason="test input"
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) == 1
        assert image_parts[0].mime_type == "image/png"

    def test_large_image_gets_split(self):
        """Image exceeding model limits must be split into multiple parts."""
        doc = ConcreteDocument.create_root(name="tall.jpg", content=_make_image_bytes(1000, 5000), reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        text_parts = [p for p in parts if isinstance(p, TextContent)]
        combined_text = "".join(p.text for p in text_parts)

        assert len(image_parts) >= 2, f"Expected 2+ image parts for tall image, got {len(image_parts)}"
        assert "split into" in combined_text.lower()

    def test_large_image_parts_are_valid(self):
        """Each split image part must be a valid image."""
        doc = ConcreteDocument.create_root(name="tall.jpg", content=_make_image_bytes(1000, 5000), reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        for i, part in enumerate(image_parts):
            img = Image.open(BytesIO(part.data))
            assert img.size[0] > 0 and img.size[1] > 0, f"Part {i} is not a valid image"

    def test_image_content_base64_serialization(self):
        """ImageContent must serialize to base64 correctly (JSON round-trip)."""
        doc = ConcreteDocument.create_root(name="small.jpg", content=_make_image_bytes(50, 50), reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) == 1

        # Verify data survives base64 encode/decode (as the API client does)
        b64_encoded = base64.b64encode(image_parts[0].data).decode("utf-8")
        decoded = base64.b64decode(b64_encoded)
        assert decoded == image_parts[0].data


# ---------------------------------------------------------------------------
# Image/PDF documents with attachments (baselines)
# ---------------------------------------------------------------------------


class TestImageDocWithAttachments:
    """Image documents with attachments."""

    def test_image_doc_with_text_attachment(self):
        """Image doc with text attachment — text should appear in parts."""
        doc = ConcreteDocument.create_root(
            name="photo.jpg",
            content=_make_image_bytes(100, 100),
            attachments=(Attachment(name="caption.txt", content=b"Photo caption"),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))

        assert len(image_parts) >= 1
        assert "Photo caption" in combined_text

    def test_image_doc_with_image_attachment(self):
        """Image doc with another image attachment — both must be ImageContent."""
        doc = ConcreteDocument.create_root(
            name="main.jpg",
            content=_make_image_bytes(200, 200),
            attachments=(Attachment(name="thumb.jpg", content=_make_image_bytes(50, 50)),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) >= 2, f"Expected 2+ ImageContent (main + attachment), got {len(image_parts)}"


class TestPdfDocWithAttachments:
    """PDF documents with attachments."""

    def test_pdf_doc_with_image_attachment(self):
        """PDF doc with image attachment — both types should appear."""
        doc = ConcreteDocument.create_root(
            name="paper.pdf",
            content=_make_pdf_bytes(),
            attachments=(Attachment(name="figure.jpg", content=_make_image_bytes()),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        pdf_parts = [p for p in parts if isinstance(p, PDFContent)]
        image_parts = [p for p in parts if isinstance(p, ImageContent)]

        assert len(pdf_parts) >= 1, "PDF document content missing"
        assert len(image_parts) >= 1, "Image attachment missing"


# ---------------------------------------------------------------------------
# Edge cases: invalid/corrupt attachments (graceful degradation)
# ---------------------------------------------------------------------------


class TestInvalidAttachments:
    """Invalid attachments must be skipped gracefully, not crash the pipeline."""

    def test_corrupt_image_attachment_skipped(self):
        """Corrupt image bytes in attachment must not crash, text doc content preserved."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Valid text content.",
            attachments=(Attachment(name="broken.jpg", content=b"not an image at all"),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert "Valid text content." in combined_text
        # Corrupt attachment should be skipped, not crash
        image_parts = [p for p in parts if isinstance(p, ImageContent)]
        assert len(image_parts) == 0

    def test_zero_byte_image_attachment_skipped(self):
        """Empty image attachment must be skipped."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Text content.",
            attachments=(Attachment(name="empty.jpg", content=b""),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert "Text content." in combined_text

    def test_corrupt_pdf_attachment_skipped(self):
        """Invalid PDF attachment must be skipped, text doc content preserved."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Valid text.",
            attachments=(Attachment(name="broken.pdf", content=b"%PDF-1.4 broken content"),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        assert "Valid text." in combined_text
        pdf_parts = [p for p in parts if isinstance(p, PDFContent)]
        assert len(pdf_parts) == 0

    def test_valid_and_invalid_attachments_mixed(self):
        """Valid attachments must be included even when others are invalid."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Mixed attachments.",
            attachments=(
                Attachment(name="good.txt", content=b"Valid text"),
                Attachment(name="bad.jpg", content=b"corrupt image"),
                Attachment(name="good.jpg", content=_make_image_bytes()),
            ),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        combined_text = "".join(p.text for p in parts if isinstance(p, TextContent))
        image_parts = [p for p in parts if isinstance(p, ImageContent)]

        assert "Valid text" in combined_text, "Valid text attachment should be present"
        assert len(image_parts) >= 1, "Valid image attachment should be present"


class TestTextDocNoAttachments:
    """Baseline: text documents without attachments must still work."""

    def test_pure_text_document(self):
        """Text-only document produces a single TextContent."""
        doc = ConcreteDocument.create_root(name="simple.md", content="Simple content.", reason="test input")

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        assert len(parts) == 1
        assert isinstance(parts[0], TextContent)
        assert "Simple content." in parts[0].text
        assert "<document>" in parts[0].text
        assert "</document>" in parts[0].text

    def test_text_with_text_attachment_only(self):
        """Text doc with only text attachment uses single TextContent (no splitting)."""
        doc = ConcreteDocument.create_root(
            name="page.md",
            content="Main content.",
            attachments=(Attachment(name="meta.txt", content=b"Metadata"),),
            reason="test input",
        )

        parts = _document_to_content_parts(doc, ImagePreset.DEFAULT)

        # Pure text case: should be one TextContent with everything inlined
        assert len(parts) == 1
        assert isinstance(parts[0], TextContent)
        assert "Main content." in parts[0].text
        assert "Metadata" in parts[0].text
