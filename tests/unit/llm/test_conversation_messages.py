"""Tests for conversation message serialization helpers."""

import base64

from ai_pipeline_core._llm_core import CoreMessage, Role
from ai_pipeline_core._llm_core.types import ImageContent, PDFContent, TextContent
from ai_pipeline_core.documents._hashing import compute_content_sha256


def test_core_messages_to_db_span_input_uses_doc_ref() -> None:
    from ai_pipeline_core.llm._conversation_messages import _core_messages_to_db_span_input

    image_bytes = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 32)
    pdf_bytes = b"%PDF-1.7\n" + (b"0" * 32)
    messages = [
        CoreMessage(
            role=Role.USER,
            content=(
                TextContent(text="before"),
                ImageContent(data=base64.b64encode(image_bytes), mime_type="image/png"),
                PDFContent(data=base64.b64encode(pdf_bytes)),
            ),
        )
    ]

    result = _core_messages_to_db_span_input(messages)

    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                {"type": "image", "$doc_ref": compute_content_sha256(image_bytes), "media_type": "image/png"},
                {"type": "pdf", "$doc_ref": compute_content_sha256(pdf_bytes), "media_type": "application/pdf"},
            ],
        }
    ]
