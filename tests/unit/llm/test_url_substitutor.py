"""Tests for URLSubstitutor class.

New two-tier design:
- Tier 1: High-entropy strings (hex ≥66 chars, base64, base58) → prefix...suffix (10+10 chars)
- Tier 2: URLs > 80 chars after Tier 1 → prefix...suffix (50+15 chars)
- Paths: /first/.../last structural shortening
"""

import pytest

from ai_pipeline_core.llm._substitutor import URLSubstitutor

# Standard Tier 1 test values (0x + 64 hex = 66 chars, meets _T1_MIN_LENGTH)
TX_HASH = "0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
TX_HASH_2 = "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"


@pytest.fixture
def substitutor():
    """Create a fresh URLSubstitutor."""
    return URLSubstitutor()


class TestURLSubstitutor:
    """Core tests for URLSubstitutor."""

    @pytest.mark.asyncio
    async def test_prepare_extracts_urls(self, substitutor):
        # URL with tx hash → Tier 1 resolves it
        texts = [f"Check https://etherscan.io/tx/{TX_HASH}"]
        substitutor.prepare(texts)

        assert substitutor.is_prepared
        assert substitutor.pattern_count >= 1

    @pytest.mark.asyncio
    async def test_substitute_replaces_urls(self, substitutor):
        # Long URL > 80 chars triggers Tier 2
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        substitutor.prepare([f"Visit {url} today"])

        result = substitutor.substitute(f"Visit {url} today")

        assert url not in result
        assert "example.com" in result  # Domain preserved in prefix
        assert "..." in result  # Truncation marker

    @pytest.mark.asyncio
    async def test_restore_reverses_substitution(self, substitutor):
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        original = f"Check {url} for info"
        substitutor.prepare([original])

        substituted = substitutor.substitute(original)
        restored = substitutor.restore(substituted)

        assert restored == original

    @pytest.mark.asyncio
    async def test_tx_hash_substitution(self, substitutor):
        text = f"Transaction: {TX_HASH}"
        substitutor.prepare([text])

        result = substitutor.substitute(text)

        assert TX_HASH not in result
        assert "0x8ccd766e" in result  # 10-char prefix preserved
        assert "8a874111e1" in result  # 10-char suffix preserved
        assert "..." in result  # Truncation marker

    @pytest.mark.asyncio
    async def test_multiple_patterns(self, substitutor):
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        text = f"URL: {url}, Transaction: {TX_HASH}"
        substitutor.prepare([text])

        result = substitutor.substitute(text)

        assert url not in result
        assert TX_HASH not in result

    @pytest.mark.asyncio
    async def test_no_patterns(self, substitutor):
        text = "Plain text with no URLs"
        substitutor.prepare([text])

        result = substitutor.substitute(text)

        assert result == text

    @pytest.mark.asyncio
    async def test_empty_text(self, substitutor):
        substitutor.prepare([""])

        result = substitutor.substitute("")

        assert result == ""

    @pytest.mark.asyncio
    async def test_unique_labels_for_different_urls(self, substitutor):
        # Two long URLs that trigger Tier 2
        texts = [
            "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page1",
            "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page2",
        ]
        substitutor.prepare(texts)

        mappings = substitutor.get_mappings()
        labels = list(mappings.values())

        # Labels should be unique
        assert len(labels) == len(set(labels))

    @pytest.mark.asyncio
    async def test_persistence_across_prepare_calls(self, substitutor):
        # First prepare
        substitutor.prepare([TX_HASH])
        label1 = substitutor.get_mappings().get(TX_HASH)

        # Second prepare with same value
        substitutor.prepare([TX_HASH])
        label2 = substitutor.get_mappings().get(TX_HASH)

        # Should use same label (deterministic)
        assert label1 == label2

    @pytest.mark.asyncio
    async def test_incremental_prepare(self, substitutor):
        substitutor.prepare([TX_HASH])
        assert substitutor.pattern_count == 1

        substitutor.prepare([TX_HASH_2])
        assert substitutor.pattern_count == 2

    @pytest.mark.asyncio
    async def test_long_hex_substitution(self, substitutor):
        """Long hex hash (66 chars) should be shortened by Tier 1."""
        # Pure hex without 0x prefix, 66 chars
        hex_hash = "8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1ab"
        text = f"Hash: {hex_hash}"
        substitutor.prepare([text])

        result = substitutor.substitute(text)

        assert hex_hash not in result
        assert "8ccd766e39" in result  # 10-char prefix
        assert "..." in result  # Truncation marker

    @pytest.mark.asyncio
    async def test_mixed_urls_and_tx_hashes(self, substitutor):
        text = f"""
        Transaction {TX_HASH}
        Docs: https://docs.example.com/api/v2/contracts/very/long/path/to/resource/page
        """
        substitutor.prepare([text])

        result = substitutor.substitute(text)
        restored = substitutor.restore(result)

        assert restored == text


class TestURLSubstitutorEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_substitute_before_prepare(self):
        substitutor = URLSubstitutor()

        # Substitute before prepare should return unchanged
        result = substitutor.substitute("https://example.com/very/long/path")
        assert result == "https://example.com/very/long/path"

    @pytest.mark.asyncio
    async def test_restore_before_prepare(self):
        substitutor = URLSubstitutor()

        # Restore before prepare should return unchanged
        result = substitutor.restore("https://example.com...1234")
        assert result == "https://example.com...1234"

    @pytest.mark.asyncio
    async def test_round_trip_with_special_chars(self):
        substitutor = URLSubstitutor()

        url = "https://example.com/path?query=value&other=test#section/very/long/path/to/resource/page"
        substitutor.prepare([url])

        substituted = substitutor.substitute(url)
        restored = substitutor.restore(substituted)

        assert restored == url

    @pytest.mark.asyncio
    async def test_deterministic_labels(self):
        """Same value should always produce same label."""
        sub1 = URLSubstitutor()
        sub1.prepare([TX_HASH])
        label1 = sub1.get_mappings()[TX_HASH]

        sub2 = URLSubstitutor()
        sub2.prepare([TX_HASH])
        label2 = sub2.get_mappings()[TX_HASH]

        assert label1 == label2

    @pytest.mark.asyncio
    async def test_url_label_format(self):
        """Long URLs should follow prefix...suffix format."""
        substitutor = URLSubstitutor()
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        substitutor.prepare([url])

        label = substitutor.get_mappings()[url]
        assert label.startswith("https://")
        assert "example.com" in label
        assert "..." in label  # Ellipsis truncation marker

    @pytest.mark.asyncio
    async def test_tx_hash_label_format(self):
        """Tx hash labels should follow prefix...suffix format (10+10)."""
        substitutor = URLSubstitutor()
        substitutor.prepare([TX_HASH])

        label = substitutor.get_mappings()[TX_HASH]
        assert label.startswith("0x8ccd766e")  # 10-char prefix
        assert "..." in label  # Ellipsis truncation marker
        assert label.endswith("8a874111e1")  # 10-char suffix

    @pytest.mark.asyncio
    async def test_short_urls_not_shortened(self):
        """URLs under 40 chars should not be shortened."""
        substitutor = URLSubstitutor()
        short_url = "https://example.com/page"  # 25 chars
        substitutor.prepare([short_url])

        # Short URL should not be in mappings
        assert short_url not in substitutor.get_mappings()

        # Substitute should return unchanged
        result = substitutor.substitute(short_url)
        assert result == short_url


class TestURLWithEmbeddedPatterns:
    """Tests for URLs containing high-entropy patterns (e.g., tx hashes).

    When a URL contains a pattern that would be shortened on its own (like a tx hash),
    the URL shortening should reuse the pattern's shortened form rather than
    hashing the entire URL independently.
    """

    @pytest.mark.asyncio
    async def test_url_with_tx_hash_reuses_hash_shortening(self):
        """URL containing tx hash should reuse the hash shortening."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()

        # The URL should be shortened
        assert url in mappings
        shortened_url = mappings[url]

        # The shortened URL should contain the shortened hash form
        assert "/tx/" in shortened_url, f"URL structure should be preserved: {shortened_url}"
        assert "0x8ccd766e" in shortened_url, f"Hash 10-char prefix should be visible: {shortened_url}"

    @pytest.mark.asyncio
    async def test_url_with_tx_hash_roundtrip(self):
        """URL with embedded tx hash should round-trip correctly."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"
        text = f"Check the transaction at {url} for details"

        substitutor.prepare([text])

        substituted = substitutor.substitute(text)
        restored = substitutor.restore(substituted)

        assert restored == text

    @pytest.mark.asyncio
    async def test_url_and_standalone_hash_share_shortening(self):
        """Same hash in URL and standalone should use consistent shortening."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"
        text = f"Tx {TX_HASH} is at {url}"

        substitutor.prepare([text])

        substituted = substitutor.substitute(text)

        # Both occurrences of the hash (standalone and in URL) should use same shortening
        mappings = substitutor.get_mappings()

        # Get the standalone hash shortening
        assert TX_HASH in mappings
        shortened_hash = mappings[TX_HASH]

        # The shortened hash should appear twice in the substituted text
        assert substituted.count(shortened_hash) == 2, f"Shortened hash '{shortened_hash}' should appear twice in: {substituted}"

    @pytest.mark.asyncio
    async def test_url_with_second_tx_hash_reuses_hash_shortening(self):
        """URL containing a different tx hash should reuse the hash shortening."""
        substitutor = URLSubstitutor()
        url = f"https://polygonscan.com/tx/{TX_HASH_2}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # Should contain shortened tx hash
        assert "/tx/" in shortened_url, f"URL structure should be preserved: {shortened_url}"
        assert "0x3a4b5c6d" in shortened_url, f"Hash 10-char prefix should be visible: {shortened_url}"

    @pytest.mark.asyncio
    async def test_url_with_long_hash_path_reuses_hash_shortening(self):
        """URL with long hex hash in path should reuse hash shortening."""
        substitutor = URLSubstitutor()
        tx_hash = "8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
        url = f"https://etherscan.io/tx/0x{tx_hash}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # Should preserve /tx/ structure and show hash prefix/suffix
        assert "/tx/" in shortened_url, f"URL structure should be preserved: {shortened_url}"

    @pytest.mark.asyncio
    async def test_multiple_patterns_in_url(self):
        """URL with multiple high-entropy patterns should shorten all of them."""
        substitutor = URLSubstitutor()
        url = f"https://example.com/compare/{TX_HASH}/to/{TX_HASH_2}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # At least one hash should be shortened within the URL
        assert "0x8ccd766e" in shortened_url or "0x3a4b5c6d" in shortened_url, f"At least one hash should be shortened in URL: {shortened_url}"

    @pytest.mark.asyncio
    async def test_pattern_in_query_parameter(self):
        """Tx hash in query parameter should be shortened."""
        substitutor = URLSubstitutor()
        url = f"https://api.example.com/swap?from={TX_HASH}&to=USDC"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # Query structure preserved, hash shortened
        assert "?from=" in shortened_url, f"Query param structure should be preserved: {shortened_url}"
        assert "&to=USDC" in shortened_url, f"Other params should be preserved: {shortened_url}"
        assert "0x8ccd766e" in shortened_url, f"Hash prefix should be visible: {shortened_url}"

    @pytest.mark.asyncio
    async def test_pattern_in_fragment(self):
        """Tx hash in URL fragment should be shortened."""
        substitutor = URLSubstitutor()
        url = f"https://example.com/explorer#/tx/{TX_HASH}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # Fragment structure preserved
        assert "#/tx/" in shortened_url, f"Fragment structure should be preserved: {shortened_url}"
        assert "0x8ccd766e" in shortened_url, f"Hash prefix should be visible: {shortened_url}"

    @pytest.mark.asyncio
    async def test_url_encoded_pattern_not_matched(self):
        """URL-encoded patterns should not be matched as T1 (falls back to Tier 2)."""
        substitutor = URLSubstitutor()
        # %38 is '8', so this encodes 0x8ccd... but regex won't match through %XX
        url = "https://etherscan.io/tx/0x%38ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        assert url in mappings
        shortened_url = mappings[url]

        # Should be shortened (Tier 2 since no T1 pattern matched through encoding)
        assert "..." in shortened_url

    @pytest.mark.asyncio
    async def test_duplicate_hash_in_url_both_replaced(self):
        """Same hash appearing twice in URL should be shortened."""
        substitutor = URLSubstitutor()
        url = f"https://api.example.com/compare/{TX_HASH}/with/{TX_HASH}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        shortened_url = mappings[url]

        # URL must be shortened (either T1 inside or T2 on whole URL)
        assert len(shortened_url) < len(url), f"URL should be shorter: {shortened_url}"
        assert "..." in shortened_url

    @pytest.mark.asyncio
    async def test_restore_url_with_embedded_pattern(self):
        """Shortened URL with embedded pattern should restore correctly."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"
        text = f"Check {url} for details"

        substitutor.prepare([text])
        substituted = substitutor.substitute(text)
        restored = substitutor.restore(substituted)

        assert restored == text, f"Failed to restore: {substituted} -> {restored}"

    @pytest.mark.asyncio
    async def test_incremental_prepare_url_then_standalone(self):
        """Preparing URL first, then standalone hash should use consistent shortening."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"

        # First prepare URL
        substitutor.prepare([url])
        url_shortened = substitutor.get_mappings()[url]

        # Then prepare standalone hash
        substitutor.prepare([TX_HASH])

        # Both should use consistent hash shortening
        hash_shortened = substitutor.get_mappings()[TX_HASH]
        assert hash_shortened in url_shortened, f"URL should contain hash shortening: {url_shortened} should contain {hash_shortened}"

    @pytest.mark.asyncio
    async def test_incremental_prepare_standalone_then_url(self):
        """Preparing standalone hash first, then URL should reuse the shortening."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"

        # First prepare standalone hash
        substitutor.prepare([TX_HASH])
        hash_shortened = substitutor.get_mappings()[TX_HASH]

        # Then prepare URL
        substitutor.prepare([url])
        url_shortened = substitutor.get_mappings()[url]

        # URL should reuse the existing hash shortening
        assert hash_shortened in url_shortened, f"URL should reuse existing hash shortening: {url_shortened} should contain {hash_shortened}"

    @pytest.mark.asyncio
    async def test_already_shortened_not_reshortened(self):
        """URLs containing already-shortened patterns should not be re-shortened."""
        substitutor = URLSubstitutor()

        # First shorten a hash
        substitutor.prepare([TX_HASH])
        hash_shortened = substitutor.get_mappings()[TX_HASH]

        # Now try to shorten a URL that contains the shortened form
        fake_url = f"https://example.com/tx/{hash_shortened}"
        substitutor.prepare([fake_url])

        # The shortened form should not be re-processed (it's too short for T1)
        if fake_url in substitutor.get_mappings():
            result = substitutor.get_mappings()[fake_url]
            # Should not produce nested ... patterns
            assert result.count("...") <= 1

    @pytest.mark.asyncio
    async def test_pattern_at_url_path_end(self):
        """Pattern at the very end of URL path should be shortened."""
        substitutor = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        shortened_url = mappings[url]

        assert "0x8ccd766e" in shortened_url

    @pytest.mark.asyncio
    async def test_short_pattern_in_long_url_not_shortened(self):
        """Short patterns (below threshold) should not be shortened even in URLs."""
        substitutor = URLSubstitutor()
        # This is too short for T1 (only 20 chars after 0x)
        short_hex = "0x1234567890abcdef1234"
        url = f"https://example.com/something/path/to/resource/{short_hex}/more/path/segments/here"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        # URL should still be shortened (it's long), just not the hex part
        assert url in mappings

    @pytest.mark.asyncio
    async def test_mixed_pattern_types_in_url(self):
        """URL with two different tx hashes should be shortened."""
        substitutor = URLSubstitutor()
        url = f"https://bridge.example.com/from/{TX_HASH}/to/{TX_HASH_2}"

        substitutor.prepare([url])

        mappings = substitutor.get_mappings()
        shortened_url = mappings[url]

        # URL must be shortened
        assert len(shortened_url) < len(url), f"URL should be shorter: {shortened_url}"
        assert "..." in shortened_url
        # Round-trip must work regardless of which tier handled it
        restored = substitutor.restore(shortened_url)
        assert restored == url


class TestFalsePositives:
    """URL path segments must NOT be shortened as high-entropy/base64 strings."""

    @pytest.mark.parametrize(
        ("path", "label"),
        [
            ("/es/build/indexer/indexer-sdk/documentation/steps/transaction-stream", "documentation/steps/transaction"),
            ("/es/build/indexer/nft-aggregator/marketplaces/bluemove", "aggregator/marketplaces/bluemove"),
            ("/es/build/indexer/nft-aggregator/marketplaces/tradeport", "aggregator/marketplaces/tradeport"),
            ("/es/build/indexer/legacy/migration", "indexer/legacy/migration"),
            ("/es/network/nodes/configure/consensus-observer", "nodes/configure/consensus"),
            ("/es/network/nodes/configure/state-sync", "nodes/configure/state"),
            ("/es/network/nodes/configure/telemetry", "nodes/configure/telemetry"),
            ("/es/network/nodes/measure/important-metrics", "nodes/measure/important"),
            ("/es/network/blockchain/blockchain-deep-dive", "network/blockchain/blockchain"),
            ("/es/network/blockchain/governance", "network/blockchain/governance"),
        ],
    )
    def test_relative_path_shortened_as_path_not_base64(self, path: str, label: str):
        """Relative URL paths must be shortened as paths (/first/.../last), not as base64."""
        sub = URLSubstitutor()
        text = f"*   [Link]({path})"
        sub.prepare([text])
        result = sub.substitute(text)
        # Path should be shortened as path type
        assert path not in result, f"Path should have been shortened: {result}"
        parts = [p for p in path.split("/") if p]
        shortened = sub.get_mappings()[path]
        # First segment preserved, last segment preserved
        assert shortened.startswith(f"/{parts[0]}/"), f"First segment not preserved: {shortened}"
        assert shortened.endswith(f"/{parts[-1]}"), f"Last segment not preserved: {shortened}"
        # Round-trip works
        restored = sub.restore(result)
        assert restored == text

    def test_markdown_link_with_relative_path_shortened_correctly(self):
        """Markdown link with relative path should be shortened as path."""
        sub = URLSubstitutor()
        path = "/es/build/indexer/indexer-sdk/documentation/steps/transaction-stream"
        text = f"*   [Transaction Stream]({path})"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result, f"Path should have been shortened: {result}"
        assert "*   [Transaction Stream](" in result
        assert result.endswith(")")
        restored = sub.restore(result)
        assert restored == text

    def test_real_base64_with_slash_still_shortened(self):
        """Actual Base64 containing `/` and `+` or `=` should still be shortened when ≥66 chars."""
        sub = URLSubstitutor()
        # Long Base64 string (72 chars, has + and =)
        b64 = "SGVsbG8gV29ybGQhIFRoaXMgaXMgYSBsb25nZXIgdGVzdCBvZiBCYXNlNjQgZW5jb2Rpbmc="
        text = f"Data: {b64}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert b64 not in result, "Real Base64 should be shortened"
        assert "..." in result


class TestMissedEncodedURLs:
    """URLs with encoded content (%3A%2F%2F) must still be shortened."""

    def test_url_with_encoded_url_in_query(self):
        """URL with encoded URL as query parameter should be shortened."""
        sub = URLSubstitutor()
        url = "https://github.com/aptos-labs/aptos-docs/issues/new?labels=documentation&template=content_issue.yml&url=https%3A%2F%2Faptos.dev%2Fes%2Fbuild%2Fguides%2Fexchanges"
        sub.prepare([url])
        result = sub.substitute(url)
        assert url not in result, f"URL should have been shortened: {result}"
        assert "..." in result

    def test_url_with_encoded_spaces_and_url(self):
        """URL with encoded spaces and embedded URL in query should be shortened."""
        sub = URLSubstitutor()
        url = "https://claude.ai/new?q=Read%20from%20https%3A%2F%2Faptos.dev%2Fes%2Fbuild%2Fguides%2Fexchanges%20so%20I%20can%20ask%20questions%20about%20it"
        sub.prepare([url])
        result = sub.substitute(url)
        assert url not in result, f"URL should have been shortened: {result}"
        assert "..." in result

    def test_encoded_url_roundtrip(self):
        """Shortened encoded URL should restore correctly."""
        sub = URLSubstitutor()
        url = "https://github.com/aptos-labs/aptos-docs/issues/new?labels=documentation&template=content_issue.yml&url=https%3A%2F%2Faptos.dev%2Fes%2Fbuild%2Fguides%2Fexchanges"
        text = f"[Report]({url})"
        sub.prepare([text])
        substituted = sub.substitute(text)
        restored = sub.restore(substituted)
        assert restored == text


class TestPartiallyShortened:
    """Long URLs must be fully shortened, not left at 200-300+ chars."""

    def test_google_redirect_url_fully_shortened(self):
        """Google grounding redirect URLs should be shortened to <= 80 chars."""
        sub = URLSubstitutor()
        url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQG1G-vW3nY37WMAKEhLfIK3tYPcvi96LKvsRVZEhz5tW7J0wwWaD9l3YuBXL-6D4B0vSwgH6NpUB9stPrmV3mE-n5wkLRSi0KOVTfKNK2BNYTEo-E0gEjdu0TIwy3FLGJ-hQuywgJO_7FqWPmpUgTp8qohnXY742DHSOiwXOU0iT9kH2A6Lutl9nUywiUc49We_angyn6oyIoxijGvo0q8vzW2LLNNJlA3kn2D2OqvcH2MUZCRYxPLvSDmg2WgEhj-zYwCVUOz_8WdT_nS-HALzaub_xZAJ"
        sub.prepare([url])
        result = sub.substitute(url)
        assert len(result) <= 80, f"URL still {len(result)} chars after shortening: {result}"
        assert "..." in result

    def test_medium_google_redirect_url_shortened(self):
        """279-char Google redirect should be shortened to <= 80 chars."""
        sub = URLSubstitutor()
        url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQH6SRPQhg8UQ3oiOPtAm3MCDQDKHJpbEit42gB05lYnZ7y-w_2MI1L_fmAB_g-XVXHsEZy4nCnM7K57p18M-CBgEcmxARZF4yNtHl_tdJzRJD7XPKGfTlQoMjMXshc-OX1ABNgNbA7wipIq_akppTD4_4-qFS5maAkpza1FpWyxlSQvO5zrTOVXUC5wSOKNIcfJ7B9m25qg95dCHBA="
        sub.prepare([url])
        result = sub.substitute(url)
        assert len(result) <= 80, f"URL still {len(result)} chars after shortening: {result}"

    def test_google_redirect_roundtrip(self):
        """Shortened Google redirect URLs should restore correctly."""
        sub = URLSubstitutor()
        url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQG1G-vW3nY37WMAKEhLfIK3tYPcvi96LKvsRVZEhz5tW7J0wwWaD9l3YuBXL-6D4B0vSwgH6NpUB9stPrmV3mE-n5wkLRSi0KOVTfKNK2BNYTEo-E0gEjdu0TIwy3FLGJ-hQuywgJO_7FqWPmpUgTp8qohnXY742DHSOiwXOU0iT9kH2A6Lutl9nUywiUc49We_angyn6oyIoxijGvo0q8vzW2LLNNJlA3kn2D2OqvcH2MUZCRYxPLvSDmg2WgEhj-zYwCVUOz_8WdT_nS-HALzaub_xZAJ"
        text = f"Source: {url}"
        sub.prepare([text])
        substituted = sub.substitute(text)
        restored = sub.restore(substituted)
        assert restored == text

    def test_url_with_tx_hash_stays_short_enough(self):
        """URL with tx hash that fits under threshold should preserve structure."""
        sub = URLSubstitutor()
        url = f"https://etherscan.io/tx/{TX_HASH}"
        sub.prepare([url])
        result = sub.substitute(url)
        # Short enough to keep structure
        assert "/tx/" in result, f"URL structure should be preserved: {result}"
        assert "0x8ccd766e" in result, f"Hash prefix should be visible: {result}"
        assert len(result) <= 80


class TestKnownLimitations:
    """Documented known limitations — not bugs, but intentional trade-offs."""

    def test_url_with_backslash_escaped_parens_roundtrip(self):
        """URLs with markdown backslash-escaped parens round-trip correctly."""
        sub = URLSubstitutor()
        url = r"https://example.com/new?q=Read%20from%20https%3A%2F%2Fexample.com%2Fpage\(include%20your%20data\)%20also%20more%20text%20here"
        sub.prepare([url])
        result = sub.substitute(url)
        restored = sub.restore(result)
        assert restored == url, f"Round-trip failed: {result}"

    def test_base64_with_slash_no_plus_not_shortened(self):
        """Base64 with / but no + or = is intentionally not shortened to prevent URL path false positives."""
        sub = URLSubstitutor()
        b64_like = "AAAAAAAABBBBBBBB/CCCCCCCCDDDDDDDDEEEE"
        text = f"Token: {b64_like}"
        sub.prepare([text])
        result = sub.substitute(text)
        # Intentional false negative — accepted to prevent URL path false positives
        assert result == text

    def test_real_base64_with_slash_and_plus_still_shortened(self):
        """Base64 with both / and + should still be shortened when ≥66 chars."""
        sub = URLSubstitutor()
        # 72 chars: has both + and / to qualify as real base64
        b64 = "SGVsbG8gV29ybGQh+FRoaXMg/XMgYSBsb25nZXIgdGVzdCBvZiBCYXNlNjQgZW5jb2Rpbmc="
        text = f"Data: {b64}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert b64 not in result

    def test_phase2_fallback_preserves_standalone_pattern_mapping(self):
        """When URL falls through to Tier 2, standalone patterns still work."""
        sub = URLSubstitutor()
        long_url = f"https://very-long-domain-name.example.com/extremely/long/path/structure/{TX_HASH}/more/segments/here/that/push/over/threshold"
        text = f"Tx {TX_HASH} at {long_url}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert TX_HASH not in result
        assert "0x8ccd766e" in result
        assert long_url not in result
        restored = sub.restore(result)
        assert restored == text

    def test_short_eth_address_not_shortened(self):
        """Ethereum addresses (42 chars) are below _T1_MIN_LENGTH and should not be shortened."""
        sub = URLSubstitutor()
        eth = "0xdac17f958d2ee523a2206206994597c13d831ec7"
        text = f"Contract: {eth}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert result == text

    def test_short_solana_address_not_shortened(self):
        """Solana addresses (44 chars) are below _T1_MIN_LENGTH and should not be shortened."""
        sub = URLSubstitutor()
        sol = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        text = f"Token: {sol}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert result == text


class TestPathShortening:
    """Long relative paths inside delimiters should be shortened to /first/.../last."""

    def test_path_in_parentheses_shortened(self):
        """Markdown link path inside () should be shortened."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"*   [Telemetría]({path})"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result
        assert "/es/" in result
        assert "/telemetry)" in result
        assert "..." in result

    def test_path_in_double_quotes_shortened(self):
        """Path inside double quotes should be shortened."""
        sub = URLSubstitutor()
        path = "/es/build/indexer/legacy/migration/guide"
        text = f'href="{path}"'
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result
        assert '"/es/' in result
        assert '/guide"' in result

    def test_path_in_single_quotes_shortened(self):
        """Path inside single quotes should be shortened."""
        sub = URLSubstitutor()
        path = "/es/build/indexer/legacy/migration/guide"
        text = f"href='{path}'"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result

    def test_path_in_xml_tags_shortened(self):
        """Path inside XML tags (>...<) should be shortened."""
        sub = URLSubstitutor()
        path = "/es/network/blockchain/governance/voting"
        text = f"<link>{path}</link>"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result
        assert ">" in result and "<" in result

    def test_path_in_square_brackets_shortened(self):
        """Path inside square brackets should be shortened."""
        sub = URLSubstitutor()
        path = "/es/build/indexer/legacy/migration/guide"
        text = f"[{path}]"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result

    def test_short_path_not_shortened(self):
        """Paths shorter than 30 chars should not be shortened."""
        sub = URLSubstitutor()
        text = "(/es/a/b/c)"
        sub.prepare([text])
        result = sub.substitute(text)
        assert result == text

    def test_path_too_few_segments_not_shortened(self):
        """Paths with fewer than 3 slashes should not be shortened."""
        sub = URLSubstitutor()
        text = "(/very-long-single-segment-path-name-here)"
        sub.prepare([text])
        result = sub.substitute(text)
        assert result == text

    def test_undelimited_path_not_shortened(self):
        """Paths without surrounding delimiters should not be shortened."""
        sub = URLSubstitutor()
        text = "see /es/network/nodes/configure/telemetry for details"
        sub.prepare([text])
        result = sub.substitute(text)
        assert result == text

    def test_path_inside_url_not_shortened_as_path(self):
        """Paths inside URLs should be handled by URL shortening, not path shortening."""
        sub = URLSubstitutor()
        text = "see https://example.com/es/network/nodes/configure/telemetry"
        sub.prepare([text])
        result = sub.substitute(text)
        # URL shortened as URL, path not separately shortened
        assert "https://example.com" in result

    def test_protocol_relative_not_shortened(self):
        """Protocol-relative URLs (//...) should not be shortened as paths."""
        sub = URLSubstitutor()
        text = "(//example.com/es/network/nodes/configure/path)"
        sub.prepare([text])
        result = sub.substitute(text)
        assert "//example.com" in result

    def test_path_roundtrip(self):
        """Shortened path must restore correctly."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"[Telemetría]({path})"
        sub.prepare([text])
        substituted = sub.substitute(text)
        restored = sub.restore(substituted)
        assert restored == text

    def test_path_format_first_ellipsis_last(self):
        """Shortened path should follow /first/.../last format."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"({path})"
        sub.prepare([text])
        mappings = sub.get_mappings()
        assert path in mappings
        shortened = mappings[path]
        assert shortened.startswith("/es/")
        assert shortened.endswith("/telemetry")
        assert "..." in shortened
        # /es/.../telemetry → ['', 'es', '...', 'telemetry']
        parts = shortened.split("/")
        assert len(parts) == 4

    def test_same_path_same_shortening(self):
        """Same path appearing twice gets consistent shortening."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"({path}) and ({path})"
        sub.prepare([text])
        result = sub.substitute(text)
        shortened = sub.get_mappings()[path]
        assert result.count(shortened) == 2

    def test_multiple_different_paths(self):
        """Multiple different paths should all be shortened."""
        sub = URLSubstitutor()
        path1 = "/es/network/nodes/configure/telemetry"
        path2 = "/es/build/indexer/legacy/migration"
        text = f"({path1}) and ({path2})"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path1 not in result
        assert path2 not in result

    def test_path_coexists_with_tx_hashes(self):
        """Paths and tx hashes should coexist correctly."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"({path}) transaction {TX_HASH}"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result
        assert TX_HASH not in result
        restored = sub.restore(result)
        assert restored == text

    @pytest.mark.parametrize(
        "path",
        [
            "/es/build/indexer/indexer-sdk/documentation/steps/transaction-stream",
            "/es/build/indexer/nft-aggregator/marketplaces/bluemove",
            "/es/build/indexer/nft-aggregator/marketplaces/tradeport",
            "/es/build/indexer/legacy/migration",
            "/es/network/nodes/configure/consensus-observer",
            "/es/network/nodes/configure/state-sync",
            "/es/network/nodes/configure/telemetry",
            "/es/network/nodes/measure/important-metrics",
            "/es/network/blockchain/blockchain-deep-dive",
            "/es/network/blockchain/governance",
        ],
    )
    def test_real_paths_from_wrong_substitutions(self, path: str):
        """Real paths should be correctly shortened in markdown links."""
        sub = URLSubstitutor()
        text = f"*   [Link]({path})"
        sub.prepare([text])
        result = sub.substitute(text)
        assert path not in result, f"Path should have been shortened: {result}"
        parts = [p for p in path.split("/") if p]
        shortened = sub.get_mappings()[path]
        assert shortened.startswith(f"/{parts[0]}/"), f"First segment not preserved: {shortened}"
        assert shortened.endswith(f"/{parts[-1]}"), f"Last segment not preserved: {shortened}"
        restored = sub.restore(result)
        assert restored == text
