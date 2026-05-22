"""Tests for LLM corruption resilience in URLSubstitutor.restore().

LLMs may corrupt shortened forms in two ways:
1. Unicode ellipsis: `...` → `…` (U+2026)
2. Case change: `0x8CCD766E` → `0x8ccd766e`

restore() handles both via Unicode normalization and case-insensitive lookup.
"""

from ai_pipeline_core.llm._substitutor import URLSubstitutor


# Tier 1: tx hashes (0x + 64 hex = 66 chars)
TX_HASH = "0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
TX_HASH_2 = "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
TX_HASH_3 = "0x2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

# URLs containing tx hashes (> 80 chars)
LONG_URL = "https://etherscan.io/tx/0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
LONG_URL_2 = "https://polygonscan.com/tx/0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"


def _prepare(sub: URLSubstitutor, *texts: str) -> dict[str, str]:
    """Prepare substitutor and return original → shortened mappings."""
    sub.prepare(list(texts))
    return sub.get_mappings()


class TestUnicodeEllipsisRestore:
    """Category 1: Unicode ellipsis normalization."""

    def test_unicode_ellipsis_restores_tx_hash(self):
        """Tx hash with `…` (Unicode ellipsis) should restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]

        # Simulate LLM converting ... to …
        corrupted = shortened.replace("...", "\u2026")
        text = f"Transaction: {corrupted}"
        restored = sub.restore(text)
        assert restored == f"Transaction: {TX_HASH}"

    def test_unicode_ellipsis_restores_url(self):
        """Long URL with `…` should restore."""
        sub = URLSubstitutor()
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        mappings = _prepare(sub, url)
        shortened = mappings[url]

        corrupted = shortened.replace("...", "\u2026")
        restored = sub.restore(corrupted)
        assert restored == url

    def test_unicode_ellipsis_restores_path(self):
        """Path with `…` should restore."""
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        text = f"({path})"
        _prepare(sub, text)
        mappings = sub.get_mappings()
        shortened = mappings[path]

        corrupted = shortened.replace("...", "\u2026")
        result = sub.restore(f"({corrupted})")
        assert result == text

    def test_unicode_ellipsis_multiple_patterns(self):
        """Multiple patterns all with `…` should all restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH, TX_HASH_2, TX_HASH_3)

        parts = []
        for orig in [TX_HASH, TX_HASH_2, TX_HASH_3]:
            corrupted = mappings[orig].replace("...", "\u2026")
            parts.append(corrupted)

        text = " ".join(parts)
        restored = sub.restore(text)
        assert TX_HASH in restored
        assert TX_HASH_2 in restored
        assert TX_HASH_3 in restored


class TestCaseInsensitiveRestore:
    """Category 2: Case-insensitive restoration."""

    def test_lowercase_hash_restores(self):
        """Lowercased shortened tx hash should restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]

        lowered = shortened.lower()
        restored = sub.restore(lowered)
        assert restored == TX_HASH

    def test_uppercase_hash_restores(self):
        """Uppercased shortened tx hash should restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]

        uppered = shortened.upper()
        restored = sub.restore(uppered)
        assert restored == TX_HASH

    def test_case_change_url_restores(self):
        """Case-changed shortened URL should restore."""
        sub = URLSubstitutor()
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        mappings = _prepare(sub, url)
        shortened = mappings[url]

        lowered = shortened.lower()
        restored = sub.restore(lowered)
        assert restored == url


class TestCombinedCorruption:
    """Category 3: Unicode ellipsis + case change combined."""

    def test_ellipsis_plus_lowercase(self):
        """Lowercased AND ellipsis-corrupted tx hash should restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]

        corrupted = shortened.replace("...", "\u2026").lower()
        restored = sub.restore(corrupted)
        assert restored == TX_HASH

    def test_ellipsis_plus_lowercase_url(self):
        """Lowercased AND ellipsis-corrupted URL should restore."""
        sub = URLSubstitutor()
        url = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"
        mappings = _prepare(sub, url)
        shortened = mappings[url]

        corrupted = shortened.replace("...", "\u2026").lower()
        restored = sub.restore(corrupted)
        assert restored == url


class TestURLWithEmbeddedPatternsCorrupted:
    """Category 4: URLs with embedded patterns, corruption applied."""

    def test_url_embedded_tx_hash_ellipsis(self):
        """URL with inner tx hash ellipsis-corrupted should restore."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, LONG_URL)
        shortened_url = mappings[LONG_URL]

        corrupted = shortened_url.replace("...", "\u2026")
        restored = sub.restore(corrupted)
        assert restored == LONG_URL

    def test_standalone_and_url_both_corrupted(self):
        """Same hash corrupted in text and in URL should both restore."""
        sub = URLSubstitutor()
        tx = TX_HASH_2
        url = f"https://etherscan.io/tx/{tx}"
        text = f"Tx {tx} at {url}"
        mappings = _prepare(sub, text)

        short_tx = mappings[tx]
        short_url = mappings[url]
        corrupted_tx = short_tx.replace("...", "\u2026")
        corrupted_url = short_url.replace("...", "\u2026")

        result = sub.restore(f"Tx {corrupted_tx} at {corrupted_url}")
        assert tx in result
        assert url in result


class TestFalsePositiveResistance:
    """Category 5: Natural text should not be falsely restored."""

    def test_natural_text_not_falsely_restored(self):
        """Random text with hex-like chars should not be replaced."""
        sub = URLSubstitutor()
        _prepare(sub, TX_HASH)

        innocent_text = "The value 0x7a25abc1488D is unrelated"
        restored = sub.restore(innocent_text)
        assert restored == innocent_text

    def test_natural_ellipsis_not_falsely_matched(self):
        """Natural `...` in prose should not be mistaken for a shortened form."""
        sub = URLSubstitutor()
        _prepare(sub, TX_HASH)

        text = "Loading... please wait"
        restored = sub.restore(text)
        assert restored == text

    def test_natural_unicode_ellipsis_not_falsely_matched(self):
        """Natural `…` in prose should not be mistaken for a shortened form."""
        sub = URLSubstitutor()
        _prepare(sub, TX_HASH)

        text = "Loading\u2026 please wait"
        restored = sub.restore(text)
        # Unicode ellipsis gets normalized to ... but shouldn't match any pattern
        assert "Loading" in restored
        assert "please wait" in restored


class TestIntegrationWithSubstitute:
    """Category 6: Integration with substitute() — corrupted forms should not interfere."""

    def test_corrupted_form_passes_through_substitute(self):
        """Corrupted shortened form is too short for pattern regexes — substitute should not touch it."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]
        corrupted = shortened.replace("...", "\u2026")

        result = sub.substitute(corrupted)
        # The corrupted form should pass through (it's not in _forward)
        assert result == corrupted

    def test_substitute_after_corrupted_restore(self):
        """Restored original should re-shorten correctly."""
        sub = URLSubstitutor()
        mappings = _prepare(sub, TX_HASH)
        shortened = mappings[TX_HASH]

        corrupted = shortened.replace("...", "\u2026")
        restored = sub.restore(corrupted)
        assert restored == TX_HASH

        re_shortened = sub.substitute(restored)
        assert re_shortened == shortened

    def test_roundtrip_through_corruption(self):
        """Full cycle: substitute → corrupt hash only → restore → re-substitute should be stable."""
        sub = URLSubstitutor()
        text = f"Transaction {TX_HASH} confirmed"
        sub.prepare([text])

        shortened = sub.substitute(text)
        hash_short = sub.get_mappings()[TX_HASH]

        # Corrupt only the shortened hash form (not the surrounding text)
        corrupted_hash = hash_short.replace("...", "\u2026").lower()
        corrupted = shortened.replace(hash_short, corrupted_hash)

        restored = sub.restore(corrupted)
        assert TX_HASH in restored

        re_shortened = sub.substitute(restored)
        assert re_shortened == shortened
