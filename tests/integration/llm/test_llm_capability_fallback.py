"""Capability-flag fallback advancement integration test.

When an ``AIModel`` declares a capability as ``False`` and the request needs
that capability, the framework advances to the next model in the ``fallback``
chain transparently — no ``TerminalError`` reaches the caller as long as
some fallback can serve the request. End-to-end coverage of the pre-flight
gate path lives here; the deterministic unit covers the gate itself in
``tests/unit/_llm_core/test_capability_gates.py``.
"""

import base64
from typing import Any

import pytest

from ai_pipeline_core._llm_core.client import generate
from ai_pipeline_core._llm_core.request import LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, PDFContent, Role
from ai_pipeline_core.exceptions import TerminalError
from ai_pipeline_core.settings import settings
from tests.support.helpers import pdf_with_marker
from tests.support.model_catalog import ALTERNATE_TEST_MODEL, DEFAULT_TEST_MODEL

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


@pytest.mark.asyncio
async def test_pdf_request_advances_when_primary_lacks_capability(nonce: str, cost_budget: Any) -> None:
    """A primary declaring ``supports_pdfs=False`` with a PDF-capable fallback completes via the fallback."""
    primary = DEFAULT_TEST_MODEL.model_copy(update={"supports_pdfs": False, "fallback": ALTERNATE_TEST_MODEL})
    marker = f"CAPFB-{nonce}"
    pdf = PDFContent(data=base64.b64encode(pdf_with_marker(marker)), filename=f"capfb-{nonce}.pdf")
    request = LLMRequest(
        model=primary,
        messages=(
            CoreMessage(role=Role.SYSTEM, content="Answer briefly."),
            CoreMessage(role=Role.USER, content=(pdf,)),
            CoreMessage(
                role=Role.USER,
                content=f"Read the attached PDF and return the exact marker string. Nonce: {nonce}",
            ),
        ),
        purpose=f"capability-fallback-integration-{nonce}",
    )
    response = await generate(request)
    cost_budget.add(response)
    chain = response.transport.model_chain
    assert chain.requested == primary.name
    assert chain.active == ALTERNATE_TEST_MODEL.name
    assert chain.used_fallback is True
    assert marker in response.content, (
        f"fallback model should have read the marker {marker!r} from the PDF; "
        f"got content preview: {response.content[:200]!r}"
    )


@pytest.mark.asyncio
async def test_capability_chain_exhausted_raises_terminal(nonce: str) -> None:
    """When no model in the chain can serve the request, the call raises ``TerminalError``.

    No ``cost_budget`` interaction: the request must not reach any deployment.
    """
    fallback_also_incapable = ALTERNATE_TEST_MODEL.model_copy(update={"supports_pdfs": False})
    primary = DEFAULT_TEST_MODEL.model_copy(update={"supports_pdfs": False, "fallback": fallback_also_incapable})
    pdf = PDFContent(
        data=base64.b64encode(pdf_with_marker(f"EXH-{nonce}")),
        filename=f"exh-{nonce}.pdf",
    )
    request = LLMRequest(
        model=primary,
        messages=(CoreMessage(role=Role.USER, content=(pdf,)),),
        purpose=f"capability-fallback-exhausted-{nonce}",
    )
    with pytest.raises(TerminalError, match="supports_pdfs=False"):
        await generate(request)
