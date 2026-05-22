"""Tests for Conversation current_date feature."""

import json
from datetime import date
from typing import Any

import pytest

from ai_pipeline_core._llm_core import LLMRequest, Role
from ai_pipeline_core.llm import Conversation, ModelOptions
from tests.support.helpers import ConcreteDocument, create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


def _system_prompt(request: LLMRequest) -> str | None:
    first = request.messages[0] if request.messages else None
    return first.content if first is not None and first.role == Role.SYSTEM and isinstance(first.content, str) else None


class TestCurrentDateInitialization:
    """Tests for current_date field auto-initialization."""

    def test_default_sets_current_date(self):
        """Default include_date=True auto-sets current_date to today."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert conv.include_date is True
        assert conv.current_date == date.today().isoformat()

    def test_include_date_false_no_date(self):
        """include_date=False leaves current_date as None."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False)
        assert conv.include_date is False
        assert conv.current_date is None

    def test_explicit_current_date_preserved(self):
        """Explicit current_date is preserved, not overwritten by auto-init."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        assert conv.current_date == "2025-01-15"

    def test_explicit_date_with_include_false(self):
        """Explicit current_date works even with include_date=False."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False, current_date="2025-06-01")
        assert conv.current_date == "2025-06-01"


class TestCurrentDatePreservation:
    """Tests that current_date is preserved across builder methods."""

    def test_with_model_preserves_date(self):
        """with_model() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        new_conv = conv.with_model(ALTERNATE_TEST_MODEL)
        assert new_conv.current_date == "2025-01-15"

    def test_with_context_preserves_date(self):
        """with_context() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        doc = ConcreteDocument(name="doc.md", content=b"content")
        new_conv = conv.with_context(doc)
        assert new_conv.current_date == "2025-01-15"

    def test_with_document_preserves_date(self):
        """with_document() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        doc = ConcreteDocument(name="doc.md", content=b"content")
        new_conv = conv.with_document(doc)
        assert new_conv.current_date == "2025-01-15"

    def test_with_model_options_preserves_date(self):
        """with_model_options() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        new_conv = conv.with_model_options(ModelOptions(reasoning_effort="high"))
        assert new_conv.current_date == "2025-01-15"

    def test_with_substitutor_preserves_date(self):
        """with_substitutor() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        new_conv = conv.with_substitutor(False)
        assert new_conv.current_date == "2025-01-15"

    def test_with_assistant_message_preserves_date(self):
        """with_assistant_message() preserves current_date."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-01-15")
        new_conv = conv.with_assistant_message("ok")
        assert new_conv.current_date == "2025-01-15"


class TestCurrentDateInSystemPrompt:
    """Tests that current_date is injected into the system prompt during send."""

    @pytest.mark.asyncio
    async def test_date_appended_to_system_prompt(self, monkeypatch):
        """current_date is appended to the system prompt passed to generate."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False, current_date="2025-03-15")
        await conv.send("Hello")

        assert captured_prompts == ["Current date: 2025-03-15"]

    @pytest.mark.asyncio
    async def test_date_appended_after_user_system_prompt(self, monkeypatch):
        """current_date is appended after the user's system_prompt."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            enable_substitutor=False,
            current_date="2025-03-15",
            model_options=ModelOptions(system_prompt="You are helpful."),
        )
        await conv.send("Hello")

        assert captured_prompts == ["You are helpful.\n\nCurrent date: 2025-03-15"]

    @pytest.mark.asyncio
    async def test_no_date_when_include_date_false(self, monkeypatch):
        """No date is injected when include_date=False."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False, include_date=False)
        await conv.send("Hello")

        assert captured_prompts == [None]

    @pytest.mark.asyncio
    async def test_date_preserved_across_send(self, monkeypatch):
        """current_date remains the same across multiple send() calls."""
        call_count = 0

        async def fake_generate(request: LLMRequest) -> Any:
            _ = request
            nonlocal call_count
            call_count += 1
            return create_test_model_response(content=f"response-{call_count}")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False, current_date="2025-03-15")
        conv2 = await conv.send("First")
        conv3 = await conv2.send("Second")

        assert conv.current_date == "2025-03-15"
        assert conv2.current_date == "2025-03-15"
        assert conv3.current_date == "2025-03-15"

    @pytest.mark.asyncio
    async def test_date_before_substitutor_instruction(self, monkeypatch):
        """Date appears before substitutor instruction in system prompt."""
        captured_prompts: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured_prompts.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        long_hex = "0x" + "a1b2c3d4e5f6" * 12
        doc = ConcreteDocument(name="data.md", content=f"Address: {long_hex}".encode())

        # cache_ttl=0 keeps the framework auto-injection path active — with a
        # non-empty context + default cache_ttl the framework now suppresses
        # both date and substitutor guidance to avoid Vertex's cached_content
        # conflict; the ordering test still applies to the inject path.
        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            enable_substitutor=True,
            current_date="2025-03-15",
            model_options=ModelOptions(cache_ttl=0),
        )
        conv = conv.with_context(doc)
        await conv.send("Check this")

        prompt = captured_prompts[0]
        assert prompt is not None
        # Date comes before substitutor instruction
        date_pos = prompt.index("Current date: 2025-03-15")
        sub_pos = prompt.index("Text uses ... (three dots)")
        assert date_pos < sub_pos


class TestCurrentDateSerialization:
    """Tests that current_date survives JSON serialization round-trip."""

    def test_json_round_trip(self):
        """current_date and include_date survive model_dump_json / model_validate_json."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, current_date="2025-06-01")
        json_str = conv.model_dump_json()
        data = json.loads(json_str)

        assert data["current_date"] == "2025-06-01"
        assert data["include_date"] is True

    def test_json_round_trip_no_date(self):
        """include_date=False produces null current_date in JSON."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, include_date=False)
        json_str = conv.model_dump_json()
        data = json.loads(json_str)

        assert data["current_date"] is None
        assert data["include_date"] is False
