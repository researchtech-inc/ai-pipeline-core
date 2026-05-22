"""Tests for default substitutor system prompt injection in Conversation._execute_send().

When the substitutor is active with patterns and no user system prompt is provided,
Conversation should inject a default system prompt instructing the LLM to preserve ~ markers.
"""

import pytest

from pydantic import BaseModel

from ai_pipeline_core._llm_core import LLMRequest, Role
from ai_pipeline_core.llm import ModelOptions
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.llm._conversation_runtime import _SUBSTITUTOR_INSTRUCTION
from tests.support.helpers import create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL

LONG_URL = "https://example.com/docs/api/v2/reference/contracts/very/long/path/to/resource/page"


def _system_prompt(request: LLMRequest) -> str | None:
    first = request.messages[0] if request.messages else None
    return first.content if first is not None and first.role == Role.SYSTEM and isinstance(first.content, str) else None


class _StructuredResult(BaseModel):
    answer: str


class TestPromptInjection:
    """Category 1: Default prompt injection conditions."""

    @pytest.mark.asyncio
    async def test_default_prompt_injected_when_no_system_prompt(self, monkeypatch):
        """Active substitutor with patterns → default prompt should be injected."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False)
        await conv.send(f"Check {LONG_URL}")

        assert captured_prompts == [_SUBSTITUTOR_INSTRUCTION]

    @pytest.mark.asyncio
    async def test_substitutor_instruction_appended_to_user_system_prompt(self, monkeypatch):
        """User-provided system prompt should have substitutor instruction appended."""
        captured_prompts: list[str | None] = []
        user_prompt = "You are a blockchain expert."

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False, model_options=ModelOptions(system_prompt=user_prompt))
        await conv.send(f"Check {LONG_URL}")

        assert captured_prompts == [f"{user_prompt}\n\n{_SUBSTITUTOR_INSTRUCTION}"]

    @pytest.mark.asyncio
    async def test_default_prompt_not_injected_when_substitutor_disabled(self, monkeypatch):
        """Substitutor disabled → no prompt injection."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False, enable_substitutor=False)
        await conv.send(f"Check {LONG_URL}")

        assert captured_prompts == [None]

    @pytest.mark.asyncio
    async def test_default_prompt_not_injected_when_no_patterns(self, monkeypatch):
        """Substitutor active but 0 patterns → no prompt injection."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False)
        await conv.send("Plain text with no URLs or addresses")

        assert captured_prompts == [None]


class TestPromptIntegration:
    """Category 2: Integration tests for prompt injection."""

    @pytest.mark.asyncio
    async def test_default_prompt_with_model_options_none(self, monkeypatch):
        """model_options=None + patterns → creates new ModelOptions with default prompt."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False, model_options=None)
        await conv.send(f"Check {LONG_URL}")

        assert captured_prompts == [_SUBSTITUTOR_INSTRUCTION]

    @pytest.mark.asyncio
    async def test_default_prompt_does_not_mutate_original_options(self, monkeypatch):
        """Original model_options should not be modified after send."""

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        original_opts = ModelOptions(reasoning_effort="high")
        conv = Conversation(model=DEFAULT_TEST_MODEL, model_options=original_opts)
        await conv.send(f"Check {LONG_URL}")

        # Original should be unchanged
        assert original_opts.system_prompt is None
        assert original_opts.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_default_prompt_preserves_other_model_options(self, monkeypatch):
        """When injecting prompt, other model_options fields should be preserved."""
        captured_prompts: list[str | None] = []
        captured_requests: list[LLMRequest] = []

        async def fake_generate(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            captured_requests.append(request)
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False, model_options=ModelOptions(reasoning_effort="high", retries=5))
        await conv.send(f"Check {LONG_URL}")

        assert captured_prompts == [_SUBSTITUTOR_INSTRUCTION]
        assert captured_requests[0].generation.reasoning_effort == "high"
        assert captured_requests[0].retry.retries == 5

    @pytest.mark.asyncio
    async def test_default_prompt_works_with_send_structured(self, monkeypatch):
        """Default prompt should also be injected for send_structured()."""
        from tests.support.helpers import create_test_structured_model_response

        captured_prompts: list[str | None] = []

        async def fake_generate_structured(request: LLMRequest):
            captured_prompts.append(_system_prompt(request))
            assert request.response.format is _StructuredResult
            return create_test_structured_model_response(parsed=_StructuredResult(answer="OK"))

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate_structured)

        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False)
        await conv.send_structured(f"Check {LONG_URL}", _StructuredResult)

        assert captured_prompts == [_SUBSTITUTOR_INSTRUCTION]
