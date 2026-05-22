"""Integration tests for PDF documents in LLM context."""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument, TransportSpy, api_part_counts, pdf_with_marker

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class PDFMarkerResponse(BaseModel):
    """Structured extraction result for PDF marker tests."""

    marker: str = Field(description="Unique marker read from the PDF.")


class TestLLMPDF:
    """Verify PDF bytes reach and are readable by the model."""

    @pytest.mark.asyncio
    async def test_pdf_marker_readable(
        self,
        default_test_model: AIModel,
        nonce: str,
        transport_spy: TransportSpy,
        cost_budget: Any,
    ) -> None:
        """A valid PDF document is sent as a file part and read end to end."""
        marker = f"PHASE3-PDF-{nonce}"
        doc = ConcreteDocument.create_root(
            name=f"phase3-{nonce}.pdf",
            content=pdf_with_marker(marker),
            reason="phase3 pdf integration",
        )

        conv = (
            await Conversation(
                model=default_test_model,
                enable_substitutor=False,
            )
            .with_document(doc)
            .send_structured(
                f"Read the attached PDF and return the exact marker string. Nonce: {nonce}",
                response_format=PDFMarkerResponse,
                purpose=f"pdf-marker-{nonce}",
            )
        )

        cost_budget.add(conv)

        assert marker in conv.parsed.marker
        assert api_part_counts(transport_spy.recorded_calls[0].messages).get("file", 0) >= 1
