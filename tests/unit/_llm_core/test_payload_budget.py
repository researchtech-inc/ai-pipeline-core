"""Unit coverage for inline-file budget guards in ``_transport``.

Validates the exact byte boundaries of:

- ``_ensure_inline_file_budget``: per-file cap (default 25 MB).
- ``_ensure_total_inline_file_budget``: cross-request cap (per-model limit).

Both raise ``PayloadTooLargeError`` (a ``TerminalError`` subclass) so the
client surfaces them rather than retrying.
"""

import base64

import pytest

from ai_pipeline_core._llm_core import CoreMessage, Role
from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core._llm_core._transport import (
    _ensure_inline_file_budget,
    _ensure_total_inline_file_budget,
    _MAX_INLINE_FILE_BYTES,
)
from ai_pipeline_core._llm_core.exceptions import PayloadTooLargeError
from ai_pipeline_core._llm_core.types import ImageContent, PDFContent


class TestEnsureInlineFileBudget:
    """Per-file 25 MB cap."""

    def test_default_cap_is_25mb(self) -> None:
        # Documenting the cap explicitly; the boundary tests below depend on it.
        assert _MAX_INLINE_FILE_BYTES == 25_000_000

    def test_under_cap_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Tighten the cap so the test can exercise the boundary without
        # allocating real 25 MB buffers.
        monkeypatch.setattr(_transport, "_MAX_INLINE_FILE_BYTES", 1024)
        _ensure_inline_file_budget(b"x" * 1023, filename="under.bin")

    def test_at_cap_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_transport, "_MAX_INLINE_FILE_BYTES", 1024)
        # Exactly at the cap is OK (boundary inclusive).
        _ensure_inline_file_budget(b"x" * 1024, filename="exact.bin")

    def test_just_over_cap_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_transport, "_MAX_INLINE_FILE_BYTES", 1024)
        with pytest.raises(PayloadTooLargeError) as exc_info:
            _ensure_inline_file_budget(b"x" * 1025, filename="over.bin")
        message = str(exc_info.value)
        assert "1025" in message
        assert "1024" in message
        assert "'over.bin'" in message


class TestEnsureTotalInlineFileBudget:
    """Cross-request cap (per-model ``max_inline_file_total_bytes``)."""

    def _image_message(self, size: int) -> CoreMessage:
        return CoreMessage(
            role=Role.USER,
            content=(ImageContent(data=base64.b64encode(b"x" * size), mime_type="image/png"),),
        )

    def _pdf_message(self, size: int) -> CoreMessage:
        return CoreMessage(
            role=Role.USER,
            content=(PDFContent(data=base64.b64encode(b"x" * size), filename="x.pdf"),),
        )

    def test_under_budget_passes(self) -> None:
        msgs = [self._image_message(400), self._pdf_message(400)]
        _ensure_total_inline_file_budget(msgs, limit=1000)

    def test_at_budget_passes(self) -> None:
        """Total exactly equal to the limit is accepted (boundary inclusive)."""
        msgs = [self._image_message(500), self._pdf_message(500)]
        _ensure_total_inline_file_budget(msgs, limit=1000)

    def test_just_over_budget_raises(self) -> None:
        msgs = [self._image_message(500), self._pdf_message(501)]
        with pytest.raises(PayloadTooLargeError) as exc_info:
            _ensure_total_inline_file_budget(msgs, limit=1000)
        message = str(exc_info.value)
        assert "1001" in message
        assert "1000" in message

    def test_text_only_messages_ignored(self) -> None:
        """str / TextContent contributes 0 bytes to the budget."""
        msgs = [
            CoreMessage(role=Role.USER, content="some text"),
            CoreMessage(role=Role.SYSTEM, content="system"),
        ]
        # Any positive limit is fine; nothing should raise.
        _ensure_total_inline_file_budget(msgs, limit=1)

    def test_payload_too_large_is_terminal(self) -> None:
        """PayloadTooLargeError is non-retriable (TerminalError subclass)."""
        from ai_pipeline_core._llm_core.exceptions import NonRetriableError, TerminalError

        assert issubclass(PayloadTooLargeError, TerminalError)
        assert issubclass(PayloadTooLargeError, NonRetriableError)
