"""Public API exports for the prompt_contract package."""

import ai_pipeline_core.prompt_contract as pc


def test_prompt_contract_public_api_exports() -> None:
    expected = [
        "CitedText",
        "DocumentCitation",
        "Methodology",
        "OutputT",
        "PromptContract",
        "PromptResult",
        "ToolAvailability",
        "ToolBinding",
        "ValidationFailure",
    ]
    assert pc.__all__ == expected
    for name in expected:
        assert hasattr(pc, name)
