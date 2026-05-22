"""Shared token estimate helpers used across documents and LLM layers."""

import functools

import tiktoken

__all__ = [
    "CHARS_PER_TEXT_TOKEN",
    "TOKENS_PER_BINARY",
    "TOKENS_PER_IMAGE",
    "TOKENS_PER_PDF",
    "estimate_binary_tokens",
    "estimate_image_tokens",
    "estimate_message_text_tokens",
    "estimate_pdf_tokens",
    "estimate_text_tokens",
]

CHARS_PER_TEXT_TOKEN = 4
TOKENS_PER_IMAGE = 1080
TOKENS_PER_PDF = 1024
TOKENS_PER_BINARY = 1024


@functools.cache
def _get_tiktoken_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for plain text content."""
    return len(_get_tiktoken_encoding().encode(text))


def estimate_message_text_tokens(text: str) -> int:
    """Estimate tokens for chat message text using the framework's coarse heuristic."""
    return len(text) // CHARS_PER_TEXT_TOKEN


def estimate_image_tokens() -> int:
    """Estimate tokens for one image part."""
    return TOKENS_PER_IMAGE


def estimate_pdf_tokens() -> int:
    """Estimate tokens for one PDF part."""
    return TOKENS_PER_PDF


def estimate_binary_tokens() -> int:
    """Estimate tokens for non-text, non-image, non-PDF binary content."""
    return TOKENS_PER_BINARY
