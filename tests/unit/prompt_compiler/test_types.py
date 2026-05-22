"""Tests for prompt_compiler public API exports."""

import ai_pipeline_core as core
import ai_pipeline_core.prompt_compiler as pc


def test_prompt_compiler_public_api_exports() -> None:
    expected = [
        "Guide",
        "ListField",
        "MultiLineField",
        "OutputRule",
        "OutputT",
        "PromptSpec",
        "Role",
        "Rule",
        "StructuredField",
        "render_preview",
        "render_text",
    ]
    assert pc.__all__ == expected
    for name in expected:
        assert hasattr(pc, name)


def test_top_level_exports_include_prompt_compiler_symbols() -> None:
    names = [
        "Guide",
        "ListField",
        "MultiLineField",
        "OutputT",
        "OutputRule",
        "PromptSpec",
        "Role",
        "Rule",
        "StructuredField",
        "render_preview",
        "render_text",
    ]
    for name in names:
        assert name in core.__all__
        assert getattr(core, name) is getattr(pc, name)
