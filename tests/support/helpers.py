"""Test helper classes for concrete document implementations."""

import copy
import json
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid7

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import RawToolCall, TokenUsage
from ai_pipeline_core.database import SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings

import pytest


@dataclass(frozen=True, slots=True)
class RecordedTransportCall:
    """Recorded final request shape passed to the LLM transport."""

    model_name: str
    messages: list[dict[str, Any]]
    api_kwargs: dict[str, Any]


class ConcreteDocument(Document):
    """Concrete Document implementation for testing."""


class ConcreteDocument2(Document):
    """Second concrete Document subclass for testing."""


class RecordingSpanDatabase(_MemoryDatabase):
    """In-memory database that records every inserted span for assertions."""

    def __init__(self) -> None:
        """Initialize the recording span database."""
        super().__init__()
        self.inserted_spans: list[SpanRecord] = []

    async def insert_span(self, span: SpanRecord) -> None:
        """Record and persist a span."""
        self.inserted_spans.append(span)
        await super().insert_span(span)

    def spans_by_kind(self, kind: str) -> list[SpanRecord]:
        """Return inserted spans of a kind in stable order."""
        return sorted(
            [span for span in self.inserted_spans if span.kind == kind],
            key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)),
        )

    def completed_spans_by_kind(self, kind: str) -> list[SpanRecord]:
        """Return completed spans of a kind in stable order."""
        return [span for span in self.spans_by_kind(kind) if span.status == SpanStatus.COMPLETED]


@dataclass(slots=True)
class TransportSpy:
    """Spy for final LLM transport calls made through ``_transport.open_stream``."""

    monkeypatch: Any
    recorded_calls: list[RecordedTransportCall] = field(default_factory=list)
    _failures: list[BaseException] = field(default_factory=list)

    def start(self) -> TransportSpy:
        """Patch transport stream opening and return this spy."""
        original_open_stream = _transport.open_stream

        @asynccontextmanager
        async def _recording_open_stream(req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]) -> AsyncIterator[Any]:
            self.recorded_calls.append(
                RecordedTransportCall(
                    model_name=req.model.name,
                    messages=copy.deepcopy(messages),
                    api_kwargs=copy.deepcopy(api_kwargs),
                )
            )
            if self._failures:
                raise self._failures.pop(0)
            async with original_open_stream(req, messages=messages, api_kwargs=api_kwargs) as raw:
                yield raw

        self.monkeypatch.setattr(_transport, "open_stream", _recording_open_stream)
        return self

    def fail_next_attempt(self, exc: BaseException) -> None:
        """Arrange for the next captured transport attempt to fail."""
        self._failures.append(exc)


def captured_metadata(spy: TransportSpy, index: int = 0) -> dict[str, Any]:
    """Return captured AIPL metadata from a transport spy call."""
    metadata = spy.recorded_calls[index].api_kwargs["metadata"]
    if not isinstance(metadata, dict):
        raise AssertionError("Captured transport metadata was not a dictionary.")
    return metadata


def message_payload_text(messages: list[dict[str, Any]]) -> str:
    """Return all text content from captured API messages."""
    chunks: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return "\n".join(chunks)


def make_tool_call(call_id: str, function_name: str, arguments: str) -> RawToolCall:
    """Build a RawToolCall for tool-loop tests."""
    return RawToolCall(id=call_id, function_name=function_name, arguments=arguments)


def api_part_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Count typed content parts in captured API messages."""
    counts: dict[str, int] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if isinstance(part_type, str):
                counts[part_type] = counts.get(part_type, 0) + 1
    return counts


def make_text_image(text: str, width: int = 400, height: int = 200, fmt: Literal["JPEG", "PNG"] = "JPEG") -> bytes:
    """Render ``text`` centered on a solid-color image and return encoded bytes."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (width - (bbox[2] - bbox[0])) // 2
    y = (height - (bbox[3] - bbox[1])) // 2
    draw.text((x, y), text, fill="black", font=font)
    buf = BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=95)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def make_text_image_tile(texts: list[str], width: int = 600, tile_height: int = 400) -> bytes:
    """Create a tall JPEG with each text on a separate vertical tile."""
    total_height = tile_height * len(texts)
    img = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except OSError:
        font = ImageFont.load_default(size=72)
    for i, text in enumerate(texts):
        y_center = i * tile_height + tile_height // 2
        bbox = draw.textbbox((0, 0), text, font=font)
        x = (width - (bbox[2] - bbox[0])) // 2
        y = y_center - (bbox[3] - bbox[1]) // 2
        draw.text((x, y), text, fill="black", font=font)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def pdf_with_marker(marker: str) -> bytes:
    """Build a minimal text PDF containing a marker string."""
    escaped_marker = marker.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(f'BT /F1 24 Tf 72 700 Td ({escaped_marker}) Tj ET'.encode())} >>\nstream\n"
        f"BT /F1 24 Tf 72 700 Td ({escaped_marker}) Tj ET\nendstream".encode(),
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(payload)


def make_integration_context(database: _MemoryDatabase, *, run_id: str = "integration-test") -> ExecutionContext:
    """Build a span-recording execution context for direct integration calls."""
    deployment_id = uuid7()
    flow_span_id = uuid7()
    return ExecutionContext(
        run_id=run_id,
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="integration-test",
        flow_frame=FlowFrame(
            name="IntegrationFlow",
            flow_class_name="IntegrationFlow",
            step=1,
            total_steps=1,
            flow_minutes=(1.0,),
            completed_minutes=0.0,
            flow_params={},
        ),
        span_id=flow_span_id,
        current_span_id=flow_span_id,
        flow_span_id=flow_span_id,
    )


@pytest.fixture
def nonce() -> str:
    """Return a UUID7-derived nonce for cache-isolated prompts."""
    return uuid7().hex[:16]


@pytest.fixture
def unique_prefix_doc(nonce: str) -> Callable[[str], ConcreteDocument]:
    """Return a factory for unique context documents."""

    def _make(payload: str) -> ConcreteDocument:
        return ConcreteDocument.create_root(
            name=f"context-{nonce}.txt",
            content=f"NONCE-{nonce}\n{payload}",
            reason="integration cache isolation",
        )

    return _make


@pytest.fixture
def recording_db() -> RecordingSpanDatabase:
    """Return a fresh recording span database."""
    return RecordingSpanDatabase()


@pytest.fixture
def recording_context(nonce: str) -> Callable[[RecordingSpanDatabase, str | None], Generator[Any]]:
    """Return a context manager factory that installs a recording execution context."""

    @contextmanager
    def _context(database: RecordingSpanDatabase, run_id: str | None = None) -> Generator[Any]:
        ctx = make_integration_context(database, run_id=run_id or f"integration-{nonce}")
        with set_execution_context(ctx):
            yield ctx

    return _context


@pytest.fixture
def transport_spy(monkeypatch: pytest.MonkeyPatch) -> TransportSpy:
    """Return an active transport spy."""
    return TransportSpy(monkeypatch).start()


def last_model_response(conversation: Conversation) -> ModelResponse[Any]:
    """Return the last model response stored in a conversation."""
    for message in reversed(conversation.messages):
        if isinstance(message, ModelResponse):
            return message
    raise AssertionError("Conversation does not contain a ModelResponse.")


def span_meta(span: SpanRecord) -> dict[str, Any]:
    """Decode a span metadata JSON object."""
    payload = json.loads(span.meta_json or "{}")
    if not isinstance(payload, dict):
        raise AssertionError(f"Span {span.span_id} meta_json did not decode to an object.")
    return payload


def span_metrics(span: SpanRecord) -> dict[str, Any]:
    """Decode a span metrics JSON object."""
    payload = json.loads(span.metrics_json or "{}")
    if not isinstance(payload, dict):
        raise AssertionError(f"Span {span.span_id} metrics_json did not decode to an object.")
    return payload


def span_input(span: SpanRecord) -> dict[str, Any]:
    """Decode a span input JSON object."""
    payload = json.loads(span.input_json or "{}")
    if not isinstance(payload, dict):
        raise AssertionError(f"Span {span.span_id} input_json did not decode to an object.")
    return payload


def create_test_model_response(
    content: str = "Test response",
    reasoning_content: str = "",
    model: str = "test-model",
    response_id: str = "test-response-id",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    cost: float | None = None,
) -> ModelResponse[str]:
    """Create a ModelResponse[str] for testing."""
    return ModelResponse[str](
        content=content,
        parsed=content,
        reasoning_content=reasoning_content,
        citations=(),
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        cost=cost,
        model=model,
        response_id=response_id,
    )


def create_test_structured_model_response(
    parsed: BaseModel,
    content: str | None = None,
    reasoning_content: str = "",
    model: str = "test-model",
    response_id: str = "test-response-id",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    cost: float | None = None,
) -> ModelResponse[Any]:
    """Create a ModelResponse with structured output for testing."""
    if content is None:
        content = parsed.model_dump_json()
    return ModelResponse[Any](
        content=content,
        parsed=parsed,
        reasoning_content=reasoning_content,
        citations=(),
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        cost=cost,
        model=model,
        response_id=response_id,
    )
