"""Prove structured JSON output can trigger false degeneration detection.

detect_output_degeneration does not distinguish between degenerate repetition
and JSON structural whitespace. The call site in client.py does not skip
detection for structured output responses.
"""

import inspect
import json


from ai_pipeline_core._llm_core._degeneration import (
    detect_output_degeneration,
)


# ── Proving tests: PASS on current code, demonstrate the bug ─────────


class TestWhitespaceTriggersDegeneration:
    """Prove that whitespace patterns trigger false positive detection."""

    def test_consecutive_spaces_detected_as_degeneration(self) -> None:
        """300+ consecutive spaces trigger degeneration detection."""
        content = "Some prefix text\n" + " " * 350 + "\nSome suffix text"
        result = detect_output_degeneration(content)
        assert result is not None
        assert "repeated" in result

    def test_consecutive_newlines_detected(self) -> None:
        """300+ consecutive newlines trigger degeneration detection."""
        content = "prefix" + "\n" * 350 + "suffix"
        result = detect_output_degeneration(content)
        assert result is not None

    def test_json_with_trailing_whitespace_padding(self) -> None:
        """JSON with trailing whitespace (provider artifact) triggers false positive."""
        fields = {f"field_{i:03d}": f"value_{i}" for i in range(80)}
        json_text = json.dumps(fields, indent=4)
        content = json_text + "\n" + " " * 400
        result = detect_output_degeneration(content)
        assert result is not None

    def test_indented_json_with_repeated_structure(self) -> None:
        """Deeply indented JSON with repeated patterns triggers false positive."""
        # 4 spaces per indent, many levels of nesting produce long whitespace runs
        items = [{"id": i, "value": ""} for i in range(50)]
        content = json.dumps({"data": {"nested": {"items": items}}}, indent=8)
        # Add trailing padding that a provider sometimes produces
        content += " " * 500
        result = detect_output_degeneration(content)
        assert result is not None


class TestStructuredOutputSkipsDegeneration:
    """Verify the call site skips degeneration for structured output."""

    def test_call_site_has_response_format_guard(self) -> None:
        """_build_model_response_impl skips degeneration check when response_format is set."""
        from ai_pipeline_core._llm_core._response_builder import _build_model_response_impl

        source = inspect.getsource(_build_model_response_impl)

        assert "detect_output_degeneration" in source
        assert "tool_calls" in source
        assert "req.call.response.format is None" in source


# ── Regression guards: real degeneration must still be detected ──────


class TestRealDegenerationStillDetected:
    """Ensure actual degeneration is caught (regression guard)."""

    def test_word_repetition_loop(self) -> None:
        """Actual token loop is correctly detected."""
        content = "The answer is " + "bau " * 120 + "end."
        result = detect_output_degeneration(content)
        assert result is not None

    def test_normal_text_not_flagged(self) -> None:
        """Normal varied text is not flagged."""
        paragraphs = [f"Paragraph {i} discusses topic {chr(65 + i % 26)} with unique observations." for i in range(30)]
        content = "\n\n".join(paragraphs)
        result = detect_output_degeneration(content)
        assert result is None


class TestStructuredJSONNotFlagged:
    """Verify complex structured wrapper JSON is not misclassified as degeneration."""

    def test_deeply_nested_wrapper_json_not_flagged(self) -> None:
        """Deeply nested wrapper JSON with hundreds of items remains valid output."""
        items = [
            {
                "title": f"Finding {i}: {'x' * 40}",
                "description": f"Description for finding {i} with enough text to be realistic. " * 2,
                "severity": "high",
                "category": "security",
                "affected_components": [
                    {"name": f"component-{i}-a", "version": "1.0.0"},
                    {"name": f"component-{i}-b", "version": "2.3.1"},
                ],
                "recommendations": [
                    {"action": f"Fix issue {i}", "effort_hours": (i % 8) + 1, "priority": (i % 5) + 1},
                ],
                "cvss_score": 7.5,
                "is_exploitable": True,
            }
            for i in range(600)
        ]
        wrapper_json = json.dumps({"audit_findings": items}, indent=2)

        result = detect_output_degeneration(wrapper_json)
        assert result is None

    def test_array_json_not_flagged(self) -> None:
        """Top-level array JSON (no wrapper) with many complex items remains valid output."""
        items = [
            {
                "name": f"Item {i}",
                "sku": f"SKU-{i:04d}",
                "price_usd": 10.0 + i,
                "description": f"A {'very ' * 10}long description for item {i}",
                "tags": ["tag-a", "tag-b", "tag-c"],
                "in_stock": True,
            }
            for i in range(600)
        ]
        array_json = json.dumps(items, indent=2)

        result = detect_output_degeneration(array_json)
        assert result is None
