"""Preflight enforcement for AIModel capability flags.

When an ``AIModel`` declares a capability as ``False`` and a request asks for
that capability, ``_prepare_api_kwargs`` must raise ``TerminalError`` before
any transport call. Silent omission would either violate the caller contract
(tools / structured) or drop user data (images / PDFs).
"""

import base64
import struct
import zlib

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core.client import _prepare_api_kwargs
from ai_pipeline_core._llm_core.request import (
    AttemptRequest,
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
from tests.support.model_catalog import DEFAULT_TEST_MODEL


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


def _attempt(request: LLMRequest) -> AttemptRequest:
    return AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call_1")


def test_structured_output_request_against_unsupported_model_raises() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_structured_output": False})
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        response=ResponseSpec(format=_StructuredOut),
    )
    with pytest.raises(TerminalError, match="supports_structured_output=False"):
        _prepare_api_kwargs(_attempt(request))


def test_tools_request_against_unsupported_model_raises() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_tools": False})
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        tools=ToolSpec(schemas=({"type": "function", "function": {"name": "noop"}},)),
    )
    with pytest.raises(TerminalError, match="supports_tools=False"):
        _prepare_api_kwargs(_attempt(request))


def test_image_request_against_unsupported_model_raises() -> None:
    model = DEFAULT_TEST_MODEL.model_copy(update={"supports_images": False})
    image = ImageContent(data=base64.b64encode(_make_png()), mime_type="image/png")
    request = LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content=(image,)),),
    )
    with pytest.raises(TerminalError, match="supports_images=False"):
        _prepare_api_kwargs(_attempt(request))


def test_pdf_request_against_unsupported_model_raises() -> None:
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
    with pytest.raises(TerminalError, match="supports_pdfs=False"):
        _prepare_api_kwargs(_attempt(request))


def test_default_capable_model_passes_all_gates() -> None:
    """A model with all four capabilities True is never gated by preflight."""
    image = ImageContent(data=base64.b64encode(_make_png()), mime_type="image/png")
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content=(image,)),),
        response=ResponseSpec(format=_StructuredOut),
        tools=ToolSpec(schemas=({"type": "function", "function": {"name": "noop"}},)),
    )
    # Builds kwargs successfully — no TerminalError.
    api_kwargs, _key = _prepare_api_kwargs(_attempt(request))
    assert "response_format" in api_kwargs
    assert "tools" in api_kwargs
