"""Preflight enforcement for AIModel capability flags.

When an ``AIModel`` declares a capability as ``False`` and a request asks for
that capability, ``_try_one_model`` must surface a fallback-advancing outcome
before any transport call. With a ``fallback`` set, the chain advances
transparently; without one, the outer ``generate`` loop raises
``TerminalError`` so silent omission cannot violate the caller contract
(tools / structured) or drop user data (images / PDFs).
"""

import base64
import struct
import zlib

from pydantic import BaseModel

from ai_pipeline_core._llm_core.client import _preflight_capability_outcome
from ai_pipeline_core._llm_core._routing import _CallContext
from ai_pipeline_core._llm_core.request import (
    LLMRequest,
    ResponseSpec,
    ToolSpec,
)
from ai_pipeline_core._llm_core.types import (
    CoreMessage,
    ImageContent,
    PDFContent,
    Role,
)
from ai_pipeline_core.exceptions import TerminalError
from tests.support.model_catalog import ALTERNATE_TEST_MODEL, DEFAULT_TEST_MODEL


class _StructuredOut(BaseModel):
    value: int


def _make_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw = zlib.compress(b"\x00\x00\x00\x00")
    idat_crc = zlib.crc32(b"IDAT" + raw) & 0xFFFFFFFF
    idat = struct.pack(">I", len(raw)) + b"IDAT" + raw + struct.pack(">I", idat_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return sig + ihdr + idat + iend


def test_structured_output_request_against_unsupported_model_advances() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_structured_output": False})
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        response=ResponseSpec(format=_StructuredOut),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), model)
    assert outcome is not None
    assert outcome.advance_to_fallback is True
    assert isinstance(outcome.error, TerminalError)
    assert "supports_structured_output=False" in str(outcome.error)


def test_tools_request_against_unsupported_model_advances() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_tools": False})
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        tools=ToolSpec(schemas=({"type": "function", "function": {"name": "noop"}},)),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), model)
    assert outcome is not None
    assert outcome.advance_to_fallback is True
    assert isinstance(outcome.error, TerminalError)
    assert "supports_tools=False" in str(outcome.error)


def test_image_request_against_unsupported_model_advances() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_images": False})
    image = ImageContent(data=base64.b64encode(_make_png()), mime_type="image/png")
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content=(image,)),),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), model)
    assert outcome is not None
    assert outcome.advance_to_fallback is True
    assert isinstance(outcome.error, TerminalError)
    assert "supports_images=False" in str(outcome.error)


def test_pdf_request_against_unsupported_model_advances() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_pdfs": False})
    pdf_data = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000093 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n148\n%%EOF\n"
    )
    pdf = PDFContent(data=base64.b64encode(pdf_data), filename="x.pdf")
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content=(pdf,)),),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), model)
    assert outcome is not None
    assert outcome.advance_to_fallback is True
    assert isinstance(outcome.error, TerminalError)
    assert "supports_pdfs=False" in str(outcome.error)


def test_default_capable_model_passes_preflight() -> None:
    """A model declaring every capability True passes the gate (outcome is None)."""
    image = ImageContent(data=base64.b64encode(_make_png()), mime_type="image/png")
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content=(image,)),),
        response=ResponseSpec(format=_StructuredOut),
        tools=ToolSpec(schemas=({"type": "function", "function": {"name": "noop"}},)),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), DEFAULT_TEST_MODEL)
    assert outcome is None


def test_preflight_with_fallback_chain_advances() -> None:
    """Pre-flight advancement carries the original TerminalError so outer generate sees it on chain exhaustion."""
    primary = DEFAULT_TEST_MODEL.model_copy(update={"supports_pdfs": False, "fallback": ALTERNATE_TEST_MODEL})
    pdf = PDFContent(
        data=base64.b64encode(b"%PDF-1.4\n%%EOF"),
        filename="x.pdf",
    )
    request = LLMRequest(
        model=primary,
        messages=(CoreMessage(role=Role.USER, content=(pdf,)),),
    )
    outcome = _preflight_capability_outcome(_CallContext(request), primary)
    assert outcome is not None
    assert outcome.advance_to_fallback is True
    # The fallback model itself is checked in the next iteration of generate()'s
    # for-loop; this assertion verifies pre-flight on the primary advances cleanly.
    assert isinstance(outcome.error, TerminalError)
