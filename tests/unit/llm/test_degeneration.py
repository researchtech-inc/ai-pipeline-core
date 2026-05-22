"""Tests for LLM output degeneration detection."""

import pytest

from ai_pipeline_core._llm_core._degeneration import (
    _MIN_CONTENT_LENGTH,
    _MIN_REPETITION_COUNT,
    _TAIL_CHARS,
    _repetition_threshold,
    detect_output_degeneration,
)
from ai_pipeline_core.exceptions import OutputDegenerationError, LLMError


# ---------------------------------------------------------------------------
# Threshold function
# ---------------------------------------------------------------------------


class TestRepetitionThreshold:
    """Test _repetition_threshold calculation."""

    def test_single_char(self):
        assert _repetition_threshold(1) == 300

    def test_short_pattern(self):
        assert _repetition_threshold(3) == 100

    def test_medium_pattern(self):
        assert _repetition_threshold(8) == 300 // 8  # 37

    def test_floor_kicks_in_at_31(self):
        # 300 // 31 = 9, floor is 10
        assert _repetition_threshold(31) == _MIN_REPETITION_COUNT

    def test_transition_point_at_30(self):
        # 300 // 30 = 10 = floor, both agree
        assert _repetition_threshold(30) == _MIN_REPETITION_COUNT

    def test_long_pattern_uses_floor(self):
        assert _repetition_threshold(100) == _MIN_REPETITION_COUNT

    def test_threshold_never_below_floor(self):
        for length in range(1, 200):
            assert _repetition_threshold(length) >= _MIN_REPETITION_COUNT


# ---------------------------------------------------------------------------
# Detection — positive cases
# ---------------------------------------------------------------------------


class TestDetectsDegeneration:
    """Cases where degeneration should be detected."""

    def test_bau_loop(self):
        """The actual incident: 'bau' repeated thousands of times."""
        result = detect_output_degeneration("bau" * 200)
        assert result is not None
        assert "'bau'" in result
        assert "length 3" in result

    def test_single_char_loop(self):
        result = detect_output_degeneration("x" * 500)
        assert result is not None
        assert "'x'" in result
        assert "length 1" in result

    def test_html_tag_loop(self):
        result = detect_output_degeneration("<br>" * 100)
        assert result is not None
        assert "length 4" in result

    def test_phrase_loop(self):
        content = "I will submit.\n" * 30
        result = detect_output_degeneration(content)
        assert result is not None
        assert "length 15" in result

    def test_medium_pattern(self):
        pattern = "some 50-char pattern that keeps repeating over!!\n"
        assert len(pattern) == 49
        content = pattern * 15
        result = detect_output_degeneration(content)
        assert result is not None
        assert "length 49" in result

    def test_valid_prefix_then_degeneration(self):
        """Degeneration at the tail after valid content."""
        valid = "This is a perfectly normal research report. " * 50  # ~2250 chars
        degenerate = "bau" * 5000
        result = detect_output_degeneration(valid + degenerate)
        assert result is not None

    def test_newline_loop(self):
        result = detect_output_degeneration("\n" * 500)
        assert result is not None

    def test_space_loop(self):
        result = detect_output_degeneration(" " * 500)
        assert result is not None

    def test_dot_newline_loop(self):
        result = detect_output_degeneration(".\n" * 200)
        assert result is not None

    def test_markdown_rule_loop(self):
        """100 consecutive horizontal rules is degenerate, not legitimate markdown."""
        result = detect_output_degeneration("---\n" * 100)
        assert result is not None

    def test_unicode_pattern(self):
        result = detect_output_degeneration("données" * 50)
        assert result is not None

    def test_content_exactly_at_tail_size(self):
        """Content exactly _TAIL_CHARS long, all degenerate."""
        pattern = "xyz"
        reps = _TAIL_CHARS // len(pattern)
        content = pattern * reps
        assert len(content) >= _MIN_CONTENT_LENGTH
        result = detect_output_degeneration(content)
        assert result is not None

    def test_degeneration_fits_in_tail_after_long_prefix(self):
        """20K chars of valid text + degeneration at the end, within tail window."""
        valid = "Normal research content about various topics. " * 450  # ~21K chars
        degenerate = "bau" * 200
        content = valid + degenerate
        assert len(content) > _TAIL_CHARS
        result = detect_output_degeneration(content)
        assert result is not None

    def test_max_pattern_length(self):
        """100-char pattern at minimum repetition count."""
        pattern = "a" * 50 + "b" * 50
        assert len(pattern) == 100
        content = pattern * _MIN_REPETITION_COUNT
        result = detect_output_degeneration(content)
        assert result is not None


# ---------------------------------------------------------------------------
# Detection — threshold boundary cases
# ---------------------------------------------------------------------------


class TestThresholdBoundaries:
    """Test detection at exact threshold boundaries."""

    @pytest.mark.parametrize(
        ("pattern", "expected_threshold"),
        [
            ("x", 300),
            ("ab", 150),
            ("bau", 100),
            ("a" * 10, 30),
            ("a" * 30, 10),
            ("a" * 50, 10),
            ("a" * 100, 10),
        ],
        ids=["L=1", "L=2", "L=3", "L=10", "L=30", "L=50", "L=100"],
    )
    def test_at_threshold_detects(self, pattern: str, expected_threshold: int):
        """Exactly at threshold — should detect."""
        content = pattern * expected_threshold
        if len(content) < _MIN_CONTENT_LENGTH:
            content = "x" * (_MIN_CONTENT_LENGTH - len(content)) + content
        result = detect_output_degeneration(content)
        assert result is not None, f"Expected detection for pattern length {len(pattern)} at {expected_threshold} reps"

    @staticmethod
    def _unique_pattern(length: int) -> str:
        """Build a pattern of given length where every position is unique.

        Uses 'chr(i % 94 + 33)' to cycle through printable ASCII.
        No sub-pattern of length < `length` can repeat consecutively, because
        every character differs from the one `length` positions earlier only
        at the exact `length` boundary.
        """
        return "".join(chr(33 + (i % 94)) for i in range(length))

    @pytest.mark.parametrize(
        ("length", "expected_threshold"),
        [
            # Only patterns where 2*L > 100 (no multiples in scan range),
            # so below-threshold at L truly means undetectable.
            (51, 10),
            (70, 10),
            (100, 10),
        ],
        ids=["L=51", "L=70", "L=100"],
    )
    def test_below_threshold_no_detect(self, length: int, expected_threshold: int):
        """One below threshold — should NOT detect.

        Uses patterns with all-unique characters and L >= 51 so that
        2*L > 100 (max scan length). No shorter sub-pattern can match,
        and the pattern is only checked at its own length.
        """
        pattern = self._unique_pattern(length)
        reps = expected_threshold - 1
        content = pattern * reps
        # Pad to minimum content length with diverse (non-repeating) text
        if len(content) < _MIN_CONTENT_LENGTH:
            padding = "".join(f"word{i} " for i in range(100))
            content = padding[: _MIN_CONTENT_LENGTH - len(content)] + content
        result = detect_output_degeneration(content)
        assert result is None, f"Unexpected detection for pattern length {length} at {reps} reps"


# ---------------------------------------------------------------------------
# No detection — fast-path exits
# ---------------------------------------------------------------------------


class TestFastPathExits:
    """Cases that exit early before scanning."""

    def test_empty_string(self):
        assert detect_output_degeneration("") is None

    def test_short_string(self):
        assert detect_output_degeneration("hello world") is None

    def test_below_min_content_length(self):
        assert detect_output_degeneration("x" * (_MIN_CONTENT_LENGTH - 1)) is None

    def test_at_min_content_length_no_pattern(self):
        content = "The quick brown fox. " * 10
        content = content[:_MIN_CONTENT_LENGTH]
        assert detect_output_degeneration(content) is None


# ---------------------------------------------------------------------------
# No detection — legitimate content
# ---------------------------------------------------------------------------


class TestLegitimateContent:
    """Legitimate content that should not trigger detection."""

    def test_normal_english_text(self):
        text = (
            "The research methodology involved a comprehensive analysis of publicly "
            "available data sources. We examined financial statements, regulatory filings, "
            "and corporate governance documents spanning the period from 2018 to 2025. "
            "The findings indicate several key patterns in executive compensation, board "
            "composition, and strategic decision-making processes. Each data point was "
            "cross-referenced against multiple independent sources to ensure accuracy. "
            "The resulting dataset comprises over 500 unique observations across 12 "
            "distinct categories of corporate activity. Statistical analysis revealed "
            "significant correlations between governance structure and financial performance "
            "metrics, particularly in the areas of revenue growth and operating margin. "
        )
        # Repeat to get substantial length
        content = text * 5
        assert len(content) > 2000
        assert detect_output_degeneration(content) is None

    def test_markdown_single_separator(self):
        content = "# Heading\n\n" + "=" * 100 + "\n\nSome content after the separator.\n"
        content += "".join(f"Paragraph {i} discusses topic {i} in detail.\n" for i in range(20))
        assert detect_output_degeneration(content) is None

    def test_markdown_table(self):
        rows = ["| Name | Age | City |", "| --- | --- | --- |"]
        rows += [f"| Person{i} | {20 + i} | City{i} |" for i in range(20)]
        content = "\n".join(rows)
        content += "\n\nSome concluding text." * 10
        assert detect_output_degeneration(content) is None

    def test_bullet_list_short_items(self):
        """Bullet list with identical short items — below threshold."""
        content = "- TODO\n" * 20
        content += "".join(f"Section {i}: covers a unique aspect of the analysis.\n" for i in range(15))
        assert detect_output_degeneration(content) is None

    def test_code_with_repeated_returns(self):
        """Code with repeated return statements — below threshold."""
        content = "    return None\n" * 8
        content += "".join(f"def func_{i}(arg_{i}):\n    return arg_{i} * {i}\n\n" for i in range(15))
        assert detect_output_degeneration(content) is None

    def test_json_with_varying_objects(self):
        """JSON array with similar but non-identical objects."""
        objects = [f'{{"id": {i}, "name": "item_{i}", "value": {i * 10}}}' for i in range(30)]
        content = "[\n" + ",\n".join(objects) + "\n]"
        content += "\n" * 50  # some trailing whitespace
        assert detect_output_degeneration(content) is None

    def test_repeated_urls_with_different_context(self):
        """Same URL appearing multiple times but separated by different text."""
        lines = [f"Source {i}: See https://example.com/report for details about topic {i}." for i in range(30)]
        content = "\n".join(lines)
        assert detect_output_degeneration(content) is None

    def test_base64_content(self):
        """Base64-encoded data — dense but not repetitive."""
        import base64

        data = bytes(range(256)) * 4
        b64 = base64.b64encode(data).decode()
        content = f"The encoded data is: {b64}\n"
        content += "".join(f"Record {i} contains encoded payload for segment {i}.\n" for i in range(15))
        assert detect_output_degeneration(content) is None

    def test_markdown_research_report(self):
        """Realistic research report output."""
        content = """# Iteration 1 Report

## Source Catalog

### Source A: Company Financial Report 2024
- **Type**: Annual Report (PDF)
- **Credibility**: High — official SEC filing
- **Key findings**: Revenue grew 15% YoY to $2.3B

### Source B: Industry Analysis by McKinsey
- **Type**: Research Report
- **Credibility**: High — reputable consulting firm
- **Key findings**: Market expected to reach $50B by 2028

### Source C: LinkedIn Profile
- **Type**: Social Media
- **Credibility**: Medium — self-reported
- **Key findings**: Subject joined Company X in March 2021

## Research Findings

The analysis of available sources reveals several important patterns:

1. **Revenue Growth**: Company showed consistent growth across all segments
2. **Market Position**: Ranked #3 in European market by revenue
3. **Leadership Changes**: Three C-suite transitions in 18 months

### Conflicts and Gaps

| Claim | Source A | Source B | Status |
|-------|---------|---------|--------|
| Revenue $2.3B | Confirmed | Confirmed | Verified |
| Market share 12% | States 12% | States 15% | Conflicting |
| Employee count | 5,200 | Not mentioned | Single source |

## Next Steps

### High-Priority Leads
- Verify market share discrepancy between sources A and B
- Locate additional sources for employee count verification
- Check regulatory filings for Q4 2024 updates

### Search Strategies
- Use SEC EDGAR for additional filings
- Search Google News for recent press coverage
- Check Glassdoor for employee-reported information
"""
        assert detect_output_degeneration(content) is None

    def test_xml_wrapped_output(self):
        """XML-wrapped spec output — normal case."""
        content = "<result>\n"
        content += "## Analysis\n\nThe data shows clear trends in three areas:\n\n"
        content += "1. Growth in Q1-Q3\n2. Decline in Q4\n3. Recovery projected for next year\n\n"
        content += "### Details\n\n"
        content += "Each quarter showed different patterns based on market conditions.\n"
        content += "</result>"
        content += " padding " * 30
        assert detect_output_degeneration(content) is None

    def test_apologize_preamble(self):
        """Repeated apology preamble — common LLM behavior, below threshold."""
        content = "I apologize for the confusion. " * 5
        content += "".join(
            f"Finding {i}: The {['financial', 'regulatory', 'operational', 'strategic', 'market'][i % 5]} "
            f"analysis for Q{(i % 4) + 1} 20{20 + i} shows deviation of {i * 1.5:.1f}% from baseline.\n"
            for i in range(20)
        )
        assert detect_output_degeneration(content) is None

    def test_pattern_exceeding_max_length_not_checked(self):
        """101-char pattern with all unique positions, repeated — only detectable at L=101+ which is not scanned."""
        # Build a 101-char pattern where every character differs, so no sub-pattern of L<=100 repeats
        pattern = "".join(chr(ord("A") + (i % 26)) + str(i) for i in range(40))[:101]
        assert len(pattern) == 101
        content = pattern * 50  # 5050 chars
        assert detect_output_degeneration(content) is None


# ---------------------------------------------------------------------------
# No detection — degeneration outside tail window
# ---------------------------------------------------------------------------


class TestTailWindow:
    """Degeneration outside the 8K tail window should not be detected."""

    @staticmethod
    def _diverse_text(length: int) -> str:
        """Generate diverse non-repeating text of approximately the given length."""
        lines = [
            f"In paragraph {i}, we examine finding #{i * 7 + 3} from the {['quarterly', 'annual', 'interim', 'special'][i % 4]} "
            f"report dated {['January', 'March', 'June', 'September', 'November'][i % 5]} {2020 + (i % 6)}. "
            f"The data shows a {i * 2.3:.1f}% variance from the projected {['revenue', 'growth', 'margin', 'cost'][i % 4]} target, "
            f"which {'exceeds' if i % 3 == 0 else 'falls below'} the threshold set by the board in resolution #{1000 + i}.\n"
            for i in range(length // 200 + 5)
        ]
        return "".join(lines)[:length]

    def test_degeneration_in_head_valid_tail(self):
        """Degeneration only in the first part, 8K+ of valid tail text."""
        degenerate = "bau" * 5000  # 15K chars
        valid_tail = self._diverse_text(_TAIL_CHARS + 2000)
        content = degenerate + valid_tail
        assert len(valid_tail) > _TAIL_CHARS
        assert detect_output_degeneration(content) is None

    def test_degeneration_ends_exactly_at_tail_boundary(self):
        """Degeneration ends right where the tail window starts — tail is clean."""
        degenerate = "bau" * 1000
        valid_tail = self._diverse_text(_TAIL_CHARS)
        content = degenerate + valid_tail
        assert detect_output_degeneration(content) is None


# ---------------------------------------------------------------------------
# Detection — return value format
# ---------------------------------------------------------------------------


class TestReturnValueFormat:
    """Verify the explanation string format."""

    def test_short_pattern_repr(self):
        result = detect_output_degeneration("bau" * 200)
        assert result is not None
        assert "'bau'" in result
        assert "length 3" in result
        assert "repeated" in result
        assert "chars" in result

    def test_long_pattern_truncated_display(self):
        """Pattern > 40 chars should be truncated in the explanation."""
        # Use a pattern with unique chars so it's detected at L=50, not shorter
        pattern = "".join(chr(ord("a") + (i % 26)) + str(i % 10) for i in range(25))
        assert len(pattern) == 50
        content = pattern * _MIN_REPETITION_COUNT
        result = detect_output_degeneration(content)
        assert result is not None
        assert "..." in result
        assert "length 50" in result

    def test_count_and_total_chars_accurate(self):
        result = detect_output_degeneration("abc" * 150)
        assert result is not None
        # Should report at least 150 repetitions and 450 chars
        assert "150" in result or any(str(n) in result for n in range(150, 200))


# ---------------------------------------------------------------------------
# Pattern overlap / algorithm correctness
# ---------------------------------------------------------------------------


class TestPatternOverlap:
    """Test algorithm handles overlapping pattern interpretations."""

    def test_nested_pattern_finds_shortest(self):
        """'abcabc' x 60 is also 'abc' x 120 — shorter pattern found first."""
        content = "abc" * 120
        result = detect_output_degeneration(content)
        assert result is not None
        assert "length 3" in result

    def test_single_char_repeated_detected_at_length_1(self):
        """'aaa' x 200 is also 'a' x 600 — L=1 detected first."""
        content = "a" * 600
        result = detect_output_degeneration(content)
        assert result is not None
        assert "length 1" in result


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestOutputDegenerationError:
    """Test the exception class."""

    def test_is_subclass_of_llm_error(self):
        assert issubclass(OutputDegenerationError, LLMError)

    def test_can_be_caught_as_llm_error(self):
        with pytest.raises(LLMError):
            raise OutputDegenerationError("test degeneration")

    def test_can_be_caught_as_exception(self):
        with pytest.raises(Exception):
            raise OutputDegenerationError("test degeneration")

    def test_message_preserved(self):
        err = OutputDegenerationError("model=gemini-3-flash, tokens=22811: substring 'bau' repeated 14000 times")
        assert "gemini-3-flash" in str(err)
        assert "bau" in str(err)
