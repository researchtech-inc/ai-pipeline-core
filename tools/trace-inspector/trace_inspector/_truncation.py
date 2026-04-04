"""Text truncation helpers for document and conversation rendering."""

from trace_inspector._types import RenderConfig

__all__ = [
    "binary_notice",
    "should_inline_document",
    "truncate_content",
    "truncation_profile_for_task",
]

MIN_TRUNCATED_SAVINGS_CHARS = 100


def should_inline_document(size_bytes: int, config: RenderConfig) -> bool:
    """Return whether a document should be fully inlined in task markdown."""
    return size_bytes <= config.inline_document_bytes


def truncation_profile_for_task(large_document_count: int, config: RenderConfig) -> tuple[int, int]:
    """Return the truncation head/tail profile for one task packet."""
    if large_document_count > config.compact_large_document_threshold:
        return config.compact_head_chars, config.compact_tail_chars
    return config.head_chars, config.tail_chars


def truncate_content(
    text: str,
    *,
    filename: str,
    total_size_bytes: int,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Return a stable head/tail preview with a middle truncation notice."""
    if len(text) <= head_chars + tail_chars + MIN_TRUNCATED_SAVINGS_CHARS:
        return text
    head = text[:head_chars]
    tail = text[-tail_chars:]
    notice = f"\n\n[TRUNCATED — {total_size_bytes} bytes total — full content in {filename}]\n\n"
    return f"{head}{notice}{tail}"


def binary_notice(*, name: str, mime_type: str, size_bytes: int, filename: str | None = None) -> str:
    """Return a human-readable placeholder for binary or image content."""
    target = f" — see {filename}" if filename is not None else ""
    return f"[BINARY ATTACHMENT — {name} — {mime_type} — {size_bytes} bytes{target}]"
