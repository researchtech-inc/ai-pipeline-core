"""Unit tests for the cache-active SYSTEM-prompt suppression.

When prompt-cache markers will be applied, the framework must not auto-inject
the ``Current date: …`` or substitutor guidance into the SYSTEM message,
because Vertex AI's cached_content path on Gemini rejects requests that
combine cached content with any system instruction or tool config. User-set
``model_options.system_prompt`` is left as-is (explicit user choice).
"""

from typing import Any

import pytest

from ai_pipeline_core._llm_core import LLMRequest, Role
from ai_pipeline_core._llm_core.types import AIModel
from ai_pipeline_core.llm import Conversation, ModelOptions
from ai_pipeline_core.llm._request_assembly import build_effective_options
from tests.support.helpers import ConcreteDocument, create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _system_prompt(request: LLMRequest) -> str | None:
    first = request.messages[0] if request.messages else None
    if first is not None and first.role == Role.SYSTEM and isinstance(first.content, str):
        return first.content
    return None


class TestBuildEffectiveOptions:
    """Direct unit tests for the cache-active branch of build_effective_options."""

    def test_cache_inactive_injects_date(self) -> None:
        options = build_effective_options(
            model_options=None,
            current_date="2025-03-15",
            substitutor_active=False,
            cache_active=False,
        )
        assert options is not None
        assert options.system_prompt == "Current date: 2025-03-15"

    def test_cache_active_suppresses_date(self) -> None:
        options = build_effective_options(
            model_options=None,
            current_date="2025-03-15",
            substitutor_active=False,
            cache_active=True,
        )
        assert options is None

    def test_cache_active_suppresses_substitutor_guidance(self) -> None:
        options = build_effective_options(
            model_options=None,
            current_date=None,
            substitutor_active=True,
            cache_active=True,
        )
        assert options is None

    def test_cache_active_preserves_user_system_prompt(self) -> None:
        user_options = ModelOptions(system_prompt="You are a pirate.")
        options = build_effective_options(
            model_options=user_options,
            current_date="2025-03-15",
            substitutor_active=True,
            cache_active=True,
        )
        assert options is user_options, "user-set ModelOptions must be returned untouched"
        assert options.system_prompt == "You are a pirate."

    def test_cache_inactive_appends_after_user_prompt(self) -> None:
        user_options = ModelOptions(system_prompt="You are a pirate.")
        options = build_effective_options(
            model_options=user_options,
            current_date="2025-03-15",
            substitutor_active=False,
            cache_active=False,
        )
        assert options is not None
        assert options.system_prompt == "You are a pirate.\n\nCurrent date: 2025-03-15"


class TestCacheActiveForTurn:
    """Conversation._cache_active_for_turn detects all the inputs correctly."""

    def test_no_context_returns_false(self) -> None:
        # cache_ttl default is non-zero, but with no context the marker
        # cannot be applied.
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert conv._cache_active_for_turn() is False

    def test_context_with_default_ttl_returns_true(self) -> None:
        # AIModel default ``cache_ttl`` is 5 minutes.
        doc = ConcreteDocument(name="doc.md", content=b"content")
        conv = Conversation(model=DEFAULT_TEST_MODEL, context=(doc,))
        assert conv._cache_active_for_turn() is True

    def test_model_cache_ttl_zero_returns_false(self) -> None:
        doc = ConcreteDocument(name="doc.md", content=b"content")
        model = AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=0)
        conv = Conversation(model=model, context=(doc,))
        assert conv._cache_active_for_turn() is False

    def test_model_options_cache_ttl_overrides_model(self) -> None:
        doc = ConcreteDocument(name="doc.md", content=b"content")
        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            context=(doc,),
            model_options=ModelOptions(cache_ttl=0),
        )
        assert conv._cache_active_for_turn() is False

    def test_model_options_cache_ttl_enables_cache(self) -> None:
        doc = ConcreteDocument(name="doc.md", content=b"content")
        model = AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=0)
        conv = Conversation(
            model=model,
            context=(doc,),
            model_options=ModelOptions(cache_ttl=5),
        )
        assert conv._cache_active_for_turn() is True


class TestSendSuppression:
    """End-to-end through ``Conversation.send`` confirms the SYSTEM prompt path."""

    @pytest.mark.asyncio
    async def test_date_not_injected_when_cache_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        doc = ConcreteDocument(name="doc.md", content=b"cached context body")
        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            context=(doc,),
            current_date="2025-03-15",
            enable_substitutor=False,
        )
        await conv.send("Hello")

        assert captured == [None], "with non-empty context + non-zero cache_ttl, the date must not be auto-injected"

    @pytest.mark.asyncio
    async def test_user_system_prompt_remains_when_cache_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        doc = ConcreteDocument(name="doc.md", content=b"cached context body")
        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            context=(doc,),
            current_date="2025-03-15",
            enable_substitutor=False,
            model_options=ModelOptions(system_prompt="You are a pirate."),
        )
        await conv.send("Hello")

        assert captured == ["You are a pirate."], (
            "user-supplied system_prompt must reach the request unchanged; framework auto-injections stay suppressed"
        )

    @pytest.mark.asyncio
    async def test_date_injected_when_cache_disabled_by_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        doc = ConcreteDocument(name="doc.md", content=b"context body")
        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            context=(doc,),
            current_date="2025-03-15",
            enable_substitutor=False,
            model_options=ModelOptions(cache_ttl=0),
        )
        await conv.send("Hello")

        assert captured == ["Current date: 2025-03-15"], "with cache_ttl=0 the framework must inject the date as before"

    @pytest.mark.asyncio
    async def test_date_injected_when_no_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str | None] = []

        async def fake_generate(request: LLMRequest) -> Any:
            captured.append(_system_prompt(request))
            return create_test_model_response(content="ok")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(
            model=DEFAULT_TEST_MODEL,
            current_date="2025-03-15",
            enable_substitutor=False,
        )
        await conv.send("Hello")

        assert captured == ["Current date: 2025-03-15"], (
            "without context the cache marker cannot apply; date injection stays on"
        )
