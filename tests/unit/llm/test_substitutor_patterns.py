"""Tests for pattern extraction in URL/address substitution."""

import re


# Define patterns locally for testing (mirrors internal _PATTERNS with _T1_MIN_LENGTH=66)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`\[\]{}|\\^]+", re.IGNORECASE)
_HEX_PREFIXED_PATTERN = re.compile(r"\b0x[a-fA-F0-9]{64,}\b")
_HIGH_ENTROPY_PATTERN = re.compile(r"\b[A-Za-z0-9]{66,}\b")


class TestURLExtraction:
    """Tests for URL pattern extraction."""

    def test_simple_url(self):
        text = "Check https://example.com for details"
        matches = list(_URL_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group() == "https://example.com"

    def test_url_with_path(self):
        text = "See https://example.com/path/to/resource"
        matches = list(_URL_PATTERN.finditer(text))
        assert matches[0].group() == "https://example.com/path/to/resource"

    def test_url_with_query_string(self):
        text = "Link: https://example.com/search?q=test&page=1"
        matches = list(_URL_PATTERN.finditer(text))
        assert "?q=test&page=1" in matches[0].group()

    def test_url_with_fragment(self):
        text = "https://example.com/page#section"
        matches = list(_URL_PATTERN.finditer(text))
        assert matches[0].group() == "https://example.com/page#section"

    def test_url_with_port(self):
        text = "Server at https://example.com:8080/api"
        matches = list(_URL_PATTERN.finditer(text))
        assert ":8080" in matches[0].group()

    def test_http_url(self):
        text = "http://insecure.com/path"
        matches = list(_URL_PATTERN.finditer(text))
        assert matches[0].group().startswith("http://")

    def test_multiple_urls(self):
        text = "Links: https://a.com and https://b.com/path"
        matches = list(_URL_PATTERN.finditer(text))
        assert len(matches) == 2

    def test_very_long_url(self):
        long_path = "/segment" * 50
        text = f"https://example.com{long_path}"
        matches = list(_URL_PATTERN.finditer(text))
        assert len(matches) == 1
        assert len(matches[0].group()) > 400

    def test_no_urls(self):
        text = "No URLs here, just plain text."
        matches = list(_URL_PATTERN.finditer(text))
        assert len(matches) == 0


class TestHexPrefixedExtraction:
    """Tests for hex-prefixed pattern extraction (tx hashes, etc.)."""

    def test_tx_hash(self):
        # Transaction hash is 0x + 64 hex chars = 66 total
        text = "Tx: 0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
        matches = list(_HEX_PREFIXED_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group().startswith("0x")
        assert len(matches[0].group()) == 66  # 0x + 64 hex

    def test_tx_hash_lowercase(self):
        text = "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
        matches = list(_HEX_PREFIXED_PATTERN.finditer(text))
        assert len(matches) == 1

    def test_multiple_tx_hashes(self):
        text = "From 0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1 to 0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
        matches = list(_HEX_PREFIXED_PATTERN.finditer(text))
        assert len(matches) == 2

    def test_short_eth_address_not_matched(self):
        """Ethereum addresses (0x + 40 hex = 42 chars) should NOT match with {64,} minimum."""
        text = "Contract: 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
        matches = list(_HEX_PREFIXED_PATTERN.finditer(text))
        assert len(matches) == 0


class TestHighEntropyExtraction:
    """Tests for high-entropy string extraction."""

    def test_long_alphanumeric_string(self):
        # 66+ char alphanumeric string should match
        text = "Token: 8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1ab"
        matches = list(_HIGH_ENTROPY_PATTERN.finditer(text))
        assert len(matches) >= 1

    def test_short_alphanumeric_not_matched(self):
        """Strings under 66 chars should NOT match with {66,} minimum."""
        text = "Solana: 7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"
        matches = list(_HIGH_ENTROPY_PATTERN.finditer(text))
        assert len(matches) == 0


class TestEmptyAndEdgeCases:
    """Tests for edge cases."""

    def test_empty_string(self):
        for pattern in [_URL_PATTERN, _HEX_PREFIXED_PATTERN, _HIGH_ENTROPY_PATTERN]:
            matches = list(pattern.finditer(""))
            assert matches == []

    def test_whitespace_only(self):
        for pattern in [_URL_PATTERN, _HEX_PREFIXED_PATTERN, _HIGH_ENTROPY_PATTERN]:
            matches = list(pattern.finditer("   \n\t  "))
            assert matches == []
