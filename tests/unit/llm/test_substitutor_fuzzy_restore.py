"""Tests for fuzzy restore in URLSubstitutor.

Tests the fuzzy fallback pass in restore() that handles:
- Suffix dropped by LLM (prefix... instead of prefix...suffix)
- Prefix truncated by 1-2 chars from end
- Suffix truncated by 1-2 chars from start
- Combined truncation
- Ambiguity detection (multiple candidates → skip + warning)
- False positive resistance (natural ellipsis not falsely restored)
"""

import logging

import pytest

from ai_pipeline_core.llm._substitutor import URLSubstitutor


# ── Test data ─────────────────────────────────────────────────────────────────

# Tier 1: tx hashes (0x + 64 hex = 66 chars, prefix 10 + suffix 10)
TX_HASH = "0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
TX_HASH_2 = "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
TX_HASH_3 = "0x2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

# Tier 2: long URLs > 80 chars (prefix 50 + suffix 15)
LINKEDIN_URL_1 = "https://de.linkedin.com/in/sebastian-mertes-7027b937?trk=public_post-text&ref=search_result"
LINKEDIN_URL_2 = "https://de.linkedin.com/in/manuel-mueller-6300022b?trk=public_post-text&ref=search_result_v2"
LONG_DOC_URL = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"

# Tier 2 collision pair: same 50-char prefix, different suffixes
COLLISION_URL_A = "https://www.example.com/documentation/api/v2/guide/getting-started-with-authentication-tutorial"
COLLISION_URL_B = "https://www.example.com/documentation/api/v2/guide/getting-started-with-authorization-overview"


def _get_parts(shortened: str) -> tuple[str, str]:
    """Split 'prefix...suffix' into (prefix, suffix)."""
    idx = shortened.index("...")
    return shortened[:idx], shortened[idx + 3 :]


def _prepare(*originals: str) -> tuple[URLSubstitutor, dict[str, str]]:
    """Create substitutor, prepare with originals, return (sub, mappings)."""
    sub = URLSubstitutor()
    sub.prepare(list(originals))
    return sub, sub.get_mappings()


# ── Class 1: Tier 2 suffix dropped ──────────────────────────────────────────


class TestFuzzyTier2SuffixDropped:
    def test_suffix_dropped_end_of_string(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        assert sub.restore(f"{prefix}...") == LINKEDIN_URL_1

    def test_suffix_dropped_with_surrounding_text(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        result = sub.restore(f"Found at {prefix}... and more")
        assert result == f"Found at {LINKEDIN_URL_1} and more"

    def test_suffix_dropped_in_json_value(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        result = sub.restore(f'{{"url": "{prefix}..."}}')
        assert result == f'{{"url": "{LINKEDIN_URL_1}"}}'

    def test_suffix_dropped_in_markdown_link(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        result = sub.restore(f"[link]({prefix}...)")
        assert result == f"[link]({LINKEDIN_URL_1})"

    def test_suffix_dropped_before_angle_bracket(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        result = sub.restore(f"<{prefix}...>")
        assert result == f"<{LINKEDIN_URL_1}>"

    @pytest.mark.parametrize(
        "boundary",
        [
            " ",
            "\t",
            "\n",
            "\r",
            '"',
            "'",
            "`",
            ")",
            ",",
            ";",
            ":",
            "!",
            "?",
            "]",
            "}",
            ">",
            "\\",
        ],
    )
    def test_suffix_dropped_before_boundary_char(self, boundary: str):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        text = f"{prefix}...{boundary}rest"
        result = sub.restore(text)
        assert result == f"{LINKEDIN_URL_1}{boundary}rest"

    def test_multiple_urls_both_suffixes_dropped(self):
        sub, m = _prepare(LINKEDIN_URL_1, LINKEDIN_URL_2)
        p1, _ = _get_parts(m[LINKEDIN_URL_1])
        p2, _ = _get_parts(m[LINKEDIN_URL_2])
        text = f"{p1}... and {p2}..."
        result = sub.restore(text)
        assert result == f"{LINKEDIN_URL_1} and {LINKEDIN_URL_2}"

    def test_same_url_appears_twice_both_suffix_dropped(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        text = f"First: {prefix}... Second: {prefix}..."
        result = sub.restore(text)
        assert result.count(LINKEDIN_URL_1) == 2


# ── Class 2: Tier 1 suffix dropped ──────────────────────────────────────────


class TestFuzzyTier1SuffixDropped:
    def test_tx_hash_suffix_dropped(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        result = sub.restore(f"Transaction: {prefix}...")
        assert result == f"Transaction: {TX_HASH}"

    def test_tx_hash_2_suffix_dropped(self):
        sub, m = _prepare(TX_HASH_3)
        prefix, _ = _get_parts(m[TX_HASH_3])
        result = sub.restore(f"Token: {prefix}...")
        assert result == f"Token: {TX_HASH_3}"

    def test_suffix_dropped_at_end_of_string(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}...") == TX_HASH


# ── Class 3: Prefix truncated ───────────────────────────────────────────────


class TestFuzzyPrefixTruncated:
    def test_tier1_prefix_minus_1_suffix_intact(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-1]}...{suffix}") == TX_HASH

    def test_tier1_prefix_minus_2_suffix_intact(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-2]}...{suffix}") == TX_HASH

    def test_tier2_prefix_minus_1_suffix_intact(self):
        sub, m = _prepare(LONG_DOC_URL)
        prefix, suffix = _get_parts(m[LONG_DOC_URL])
        assert sub.restore(f"{prefix[:-1]}...{suffix}") == LONG_DOC_URL

    def test_tier2_prefix_minus_2_suffix_intact(self):
        sub, m = _prepare(LONG_DOC_URL)
        prefix, suffix = _get_parts(m[LONG_DOC_URL])
        assert sub.restore(f"{prefix[:-2]}...{suffix}") == LONG_DOC_URL


# ── Class 4: Suffix truncated ───────────────────────────────────────────────


class TestFuzzySuffixTruncated:
    def test_tier1_suffix_start_minus_1(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}...{suffix[1:]}") == TX_HASH

    def test_tier1_suffix_start_minus_2(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}...{suffix[2:]}") == TX_HASH

    def test_tier2_suffix_start_minus_1(self):
        sub, m = _prepare(LONG_DOC_URL)
        prefix, suffix = _get_parts(m[LONG_DOC_URL])
        assert sub.restore(f"{prefix}...{suffix[1:]}") == LONG_DOC_URL

    def test_tier2_suffix_start_minus_2(self):
        sub, m = _prepare(LONG_DOC_URL)
        prefix, suffix = _get_parts(m[LONG_DOC_URL])
        assert sub.restore(f"{prefix}...{suffix[2:]}") == LONG_DOC_URL


# ── Class 5: Combined truncation ────────────────────────────────────────────


class TestFuzzyCombinedTruncation:
    def test_prefix_minus_1_suffix_minus_1(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-1]}...{suffix[1:]}") == TX_HASH

    def test_prefix_minus_1_suffix_dropped(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"Transaction: {prefix[:-1]}...") == f"Transaction: {TX_HASH}"

    def test_prefix_minus_2_suffix_dropped(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"Transaction: {prefix[:-2]}...") == f"Transaction: {TX_HASH}"

    def test_prefix_minus_1_suffix_minus_2(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-1]}...{suffix[2:]}") == TX_HASH

    def test_prefix_minus_2_suffix_minus_1(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-2]}...{suffix[1:]}") == TX_HASH

    def test_prefix_minus_2_suffix_minus_2(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-2]}...{suffix[2:]}") == TX_HASH


# ── Class 6: Ambiguity detection ─────────────────────────────────────────────


class TestFuzzyAmbiguityDetection:
    def test_same_prefix_suffix_dropped_not_restored(self, caplog):
        """Two URLs with same 50-char prefix, both suffix dropped → ambiguous."""
        sub, m = _prepare(COLLISION_URL_A, COLLISION_URL_B)
        prefix_a, _ = _get_parts(m[COLLISION_URL_A])
        prefix_b, _ = _get_parts(m[COLLISION_URL_B])
        assert prefix_a.lower() == prefix_b.lower()  # Confirm same prefix

        mangled = f"See {prefix_a}..."
        with caplog.at_level(logging.WARNING):
            result = sub.restore(mangled)
        assert result == mangled  # NOT restored
        assert "Ambiguous fuzzy restore" in caplog.text

    def test_collision_with_partial_suffix_resolves(self):
        """Collision pair with partially-present suffix → disambiguates → restores."""
        sub, m = _prepare(COLLISION_URL_A, COLLISION_URL_B)
        prefix_a, suffix_a = _get_parts(m[COLLISION_URL_A])
        # Truncate 1 char from start of suffix — still unique enough to disambiguate
        mangled = f"{prefix_a}...{suffix_a[1:]}"
        assert sub.restore(mangled) == COLLISION_URL_A

    def test_truncated_prefix_still_ambiguous(self, caplog):
        """Collision pair with -2 prefix and suffix dropped → still ambiguous."""
        sub, m = _prepare(COLLISION_URL_A, COLLISION_URL_B)
        prefix, _ = _get_parts(m[COLLISION_URL_A])
        mangled = f"{prefix[:-2]}..."
        with caplog.at_level(logging.WARNING):
            result = sub.restore(mangled)
        assert result == mangled
        assert "Ambiguous" in caplog.text


# ── Class 7: False positive resistance ──────────────────────────────────────


class TestFuzzyFalsePositiveResistance:
    def test_loading_ellipsis_unchanged(self):
        sub, _ = _prepare(TX_HASH, LINKEDIN_URL_1)
        assert sub.restore("Loading...") == "Loading..."

    def test_value_increased_ellipsis_unchanged(self):
        sub, _ = _prepare(TX_HASH, LINKEDIN_URL_1)
        assert sub.restore("The value increased...") == "The value increased..."

    def test_multiple_natural_ellipsis_unchanged(self):
        sub, _ = _prepare(TX_HASH, LINKEDIN_URL_1)
        text = "Step 1... Step 2... Step 3..."
        assert sub.restore(text) == text

    def test_prefix_with_wrong_text_after_dots_not_restored(self):
        """prefix...WRONGTEXT (non-boundary, non-suffix) → NOT restored."""
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        text = f"{prefix}...WRONG_STUFF_HERE"
        assert sub.restore(text) == text

    def test_short_hex_before_dots_not_matched(self):
        """Hex prefix shorter than 8 chars before ... → NOT matched."""
        sub, _ = _prepare(TX_HASH)
        assert sub.restore("0x7a25...") == "0x7a25..."

    def test_prose_with_registered_entries_unchanged(self):
        sub, _ = _prepare(TX_HASH, LINKEDIN_URL_1)
        text = "The Ethereum blockchain is a decentralized platform."
        assert sub.restore(text) == text


# ── Class 8: Mixed exact and fuzzy ──────────────────────────────────────────


class TestFuzzyMixedExactAndFuzzy:
    def test_tier1_exact_and_tier2_fuzzy_same_text(self):
        sub, m = _prepare(TX_HASH, LINKEDIN_URL_1)
        tx_short = m[TX_HASH]
        url_prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        text = f"Hash {tx_short} at {url_prefix}..."
        result = sub.restore(text)
        assert result == f"Hash {TX_HASH} at {LINKEDIN_URL_1}"

    def test_two_tier1_one_exact_one_fuzzy(self):
        sub, m = _prepare(TX_HASH, TX_HASH_2)
        short_1 = m[TX_HASH]
        prefix_2, _ = _get_parts(m[TX_HASH_2])
        text = f"First: {short_1} Second: {prefix_2}..."
        result = sub.restore(text)
        assert result == f"First: {TX_HASH} Second: {TX_HASH_2}"

    def test_path_exact_and_tier1_fuzzy_no_interference(self):
        sub = URLSubstitutor()
        path = "/es/network/nodes/configure/telemetry"
        sub.prepare([f"({path})", TX_HASH])
        m = sub.get_mappings()
        path_short = m[path]
        tx_prefix, _ = _get_parts(m[TX_HASH])
        text = f"({path_short}) and {tx_prefix}..."
        result = sub.restore(text)
        assert path in result
        assert TX_HASH in result

    def test_exact_match_wins_when_both_possible(self):
        """Full shortened form → exact match handles it, fuzzy is not needed."""
        sub, m = _prepare(TX_HASH)
        short = m[TX_HASH]
        assert sub.restore(short) == TX_HASH


# ── Class 9: Dots variants ──────────────────────────────────────────────────


class TestFuzzyDotsVariants:
    def test_four_dots_suffix_intact(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}....{suffix}") == TX_HASH

    def test_four_dots_suffix_dropped(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}....") == TX_HASH

    def test_five_dots_suffix_dropped(self):
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}.....") == TX_HASH

    def test_unicode_ellipsis_suffix_dropped(self):
        """Unicode ellipsis (…) with suffix dropped → normalized to ... then fuzzy restores."""
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix}\u2026") == TX_HASH

    def test_unicode_ellipsis_prefix_truncated(self):
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix[:-1]}\u2026{suffix}") == TX_HASH


# ── Class 10: Round-trip stability ──────────────────────────────────────────


class TestFuzzyRoundTrip:
    def test_tier1_suffix_dropped_roundtrip(self):
        """mangle → restore → substitute = original shortened form."""
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        restored = sub.restore(f"{prefix}...")
        assert restored == TX_HASH
        assert sub.substitute(restored) == m[TX_HASH]

    def test_tier2_suffix_dropped_roundtrip(self):
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        restored = sub.restore(f"{prefix}...")
        assert restored == LINKEDIN_URL_1
        assert sub.substitute(restored) == m[LINKEDIN_URL_1]

    def test_mixed_exact_fuzzy_roundtrip(self):
        sub, m = _prepare(TX_HASH, LINKEDIN_URL_1)
        tx_short = m[TX_HASH]
        url_prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        text = f"{tx_short} and {url_prefix}..."
        restored = sub.restore(text)
        re_shortened = sub.substitute(restored)
        assert re_shortened == f"{tx_short} and {m[LINKEDIN_URL_1]}"


# ── Class 11: Production regression ─────────────────────────────────────────


class TestFuzzyRegressionProductionBug:
    def test_linkedin_outbound_links_json(self):
        """Reproduce production bug: LinkedIn URLs with suffix dropped in JSON payload."""
        sub, m = _prepare(LINKEDIN_URL_1, LINKEDIN_URL_2)
        p1, _ = _get_parts(m[LINKEDIN_URL_1])
        p2, _ = _get_parts(m[LINKEDIN_URL_2])
        json_text = f'{{"outbound_links": ["{p1}...", "{p2}..."]}}'
        result = sub.restore(json_text)
        assert result == f'{{"outbound_links": ["{LINKEDIN_URL_1}", "{LINKEDIN_URL_2}"]}}'

    def test_linkedin_urls_in_markdown_report(self):
        sub, m = _prepare(LINKEDIN_URL_1, LINKEDIN_URL_2)
        p1, _ = _get_parts(m[LINKEDIN_URL_1])
        p2, _ = _get_parts(m[LINKEDIN_URL_2])
        text = f"Sebastian Mertes: {p1}...\nManuel Mueller: {p2}..."
        result = sub.restore(text)
        assert result == f"Sebastian Mertes: {LINKEDIN_URL_1}\nManuel Mueller: {LINKEDIN_URL_2}"


# ── Class 12: Edge cases ────────────────────────────────────────────────────


class TestFuzzyEdgeCases:
    def test_empty_text_passthrough(self):
        sub, _ = _prepare(TX_HASH)
        assert sub.restore("") == ""

    def test_no_fuzzy_entries_passthrough(self):
        sub = URLSubstitutor()
        assert sub.restore("some text with...dots") == "some text with...dots"

    def test_case_change_plus_suffix_dropped(self):
        """Uppercased prefix + suffix dropped → restores (case-insensitive matching)."""
        sub, m = _prepare(TX_HASH)
        prefix, _ = _get_parts(m[TX_HASH])
        assert sub.restore(f"{prefix.upper()}...") == TX_HASH

    def test_backslash_before_quote_in_json(self):
        r"""JSON escaped quote after dots: prefix...\" → suffix dropped → restored."""
        sub, m = _prepare(LINKEDIN_URL_1)
        prefix, _ = _get_parts(m[LINKEDIN_URL_1])
        text = f'{prefix}...\\"'
        result = sub.restore(text)
        assert LINKEDIN_URL_1 in result

    def test_excessive_truncation_not_restored(self):
        """Prefix -3 → below 8-char threshold → NOT restored."""
        sub, m = _prepare(TX_HASH)
        prefix, suffix = _get_parts(m[TX_HASH])
        mangled = f"{prefix[:-3]}...{suffix}"
        assert sub.restore(mangled) == mangled


# ── Class 13: Collision-adjusted prefix exceeds context window ────────────────


class TestFuzzyCollisionAdjustedPrefix:
    def test_collision_adjusted_prefix_fuzzy_restore(self):
        """Tier 2 URLs with collision-adjusted prefix up to 62 chars must fuzzy-restore."""
        # 7 URLs sharing first 62 chars and last 15 chars force escalation:
        # adj_prefix = 50, 52, 54, 56, 58, 60, 62
        shared_prefix = "https://www.example.com/documentation/api/v2/guide/long-path-e"  # 62 chars
        shared_suffix = "final-ending-xx"  # 15 chars
        urls = [f"{shared_prefix}{chr(ord('a') + i) * 4}-middle{shared_suffix}" for i in range(7)]

        sub = URLSubstitutor()
        sub.prepare(urls)
        m = sub.get_mappings()

        last_url = urls[-1]
        shortened = m[last_url]
        prefix, suffix = _get_parts(shortened)
        assert len(prefix) == 62  # Verify collision escalation happened

        # Drop suffix → fuzzy should restore
        assert sub.restore(f"{prefix}...") == last_url

    def test_collision_adjusted_prefix_with_surrounding_text(self):
        """Same bug but with surrounding text so context window has preceding chars."""
        shared_prefix = "https://www.example.com/documentation/api/v2/guide/long-path-e"
        shared_suffix = "final-ending-xx"
        urls = [f"{shared_prefix}{chr(ord('a') + i) * 4}-middle{shared_suffix}" for i in range(7)]

        sub = URLSubstitutor()
        sub.prepare(urls)
        m = sub.get_mappings()

        last_url = urls[-1]
        prefix, _ = _get_parts(m[last_url])
        text = f"See {prefix}... for details"
        result = sub.restore(text)
        assert result == f"See {last_url} for details"


# ── Class 14: URL delimiter boundary chars ────────────────────────────────────


class TestFuzzyURLDelimiterBoundary:
    def test_suffix_dropped_before_ampersand_in_url(self):
        """T1 hash suffix dropped before & in query string → must restore."""
        sub = URLSubstitutor()
        url = f"https://api.example.com/swap?from={TX_HASH_2}&to=USDC"
        sub.prepare([url])
        m = sub.get_mappings()
        hash_short = m[TX_HASH_2]
        url_short = m[url]
        hash_prefix, _ = _get_parts(hash_short)

        mangled = url_short.replace(hash_short, f"{hash_prefix}...")
        result = sub.restore(mangled)
        assert result == url

    def test_suffix_dropped_before_slash_in_url(self):
        """T1 hash suffix dropped before / in URL path → must restore."""
        sub = URLSubstitutor()
        url = f"https://etherscan.io/token/{TX_HASH_2}/details"
        sub.prepare([url])
        m = sub.get_mappings()
        hash_short = m[TX_HASH_2]
        url_short = m[url]
        hash_prefix, _ = _get_parts(hash_short)

        mangled = url_short.replace(hash_short, f"{hash_prefix}...")
        result = sub.restore(mangled)
        assert result == url

    def test_suffix_dropped_before_question_mark_in_url(self):
        """T1 hash suffix dropped before ? in URL → must restore."""
        sub = URLSubstitutor()
        url = f"https://etherscan.io/address/{TX_HASH_2}?tab=transactions"
        sub.prepare([url])
        m = sub.get_mappings()
        hash_short = m[TX_HASH_2]
        url_short = m[url]
        hash_prefix, _ = _get_parts(hash_short)

        mangled = url_short.replace(hash_short, f"{hash_prefix}...")
        result = sub.restore(mangled)
        assert result == url

    def test_suffix_dropped_before_hash_in_url(self):
        """T1 hash suffix dropped before # in URL fragment → must restore."""
        sub = URLSubstitutor()
        url = f"https://etherscan.io/address/{TX_HASH_2}#events"
        sub.prepare([url])
        m = sub.get_mappings()
        hash_short = m[TX_HASH_2]
        url_short = m[url]
        hash_prefix, _ = _get_parts(hash_short)

        mangled = url_short.replace(hash_short, f"{hash_prefix}...")
        result = sub.restore(mangled)
        assert result == url
