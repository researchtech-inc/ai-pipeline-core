"""Tests for eager restore of shortened URLs in Conversation responses."""

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core import CoreMessage, LLMRequest, Role
from ai_pipeline_core.llm._substitutor import URLSubstitutor
from ai_pipeline_core.llm.conversation import Conversation
from tests.support.helpers import create_test_model_response, create_test_structured_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL

# URLs with tx hashes (>80 chars, contain 66-char T1 patterns)
LONG_URL = "https://etherscan.io/tx/0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1"
LONG_URL_2 = "https://polygonscan.com/tx/0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"
TX_HASH = "0x2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class ItemList(BaseModel):
    items: list[str]
    summary: str


def _prepare_and_get_short(sub: URLSubstitutor, *originals: str) -> dict[str, str]:
    """Prepare substitutor with originals and return mapping of original -> shortened."""
    sub.prepare(list(originals))
    mappings = sub.get_mappings()
    return {orig: mappings[orig] for orig in originals if orig in mappings}


def _get_shortened_forms(*originals: str) -> dict[str, str]:
    """Create a fresh substitutor, prepare it with originals, and return mappings.

    The substitutor algorithm is deterministic, so a standalone instance
    produces the same shortened forms as the one created inside _execute_send().
    """
    sub = URLSubstitutor()
    return _prepare_and_get_short(sub, *originals)


class TestEagerRestoreSend:
    """Tests for eager restore in Conversation.send()."""

    @pytest.mark.asyncio
    async def test_content_restored_after_send(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL)
        short = shorts[LONG_URL]

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content=f"Found: {short}")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send(f"Check this: {LONG_URL}")
        assert conv2.content == f"Found: {LONG_URL}"

    @pytest.mark.asyncio
    async def test_parsed_string_restored_after_send(self, monkeypatch):
        """For unstructured responses, parsed == content (both restored)."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL)
        short = shorts[LONG_URL]

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content=f"Link: {short}")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send(f"Check this: {LONG_URL}")
        last_response = conv2.messages[-1]
        assert last_response.parsed == f"Link: {LONG_URL}"
        assert last_response.content == last_response.parsed

    @pytest.mark.asyncio
    async def test_multiple_urls_restored(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL, LONG_URL_2, TX_HASH)

        response_text = " ".join(shorts.values())

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content=response_text)

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send(f"Check: {LONG_URL} {LONG_URL_2} {TX_HASH}")
        for original in shorts:
            assert original in conv2.content
        for short in shorts.values():
            assert short not in conv2.content

    @pytest.mark.asyncio
    async def test_no_restore_when_no_patterns_matched(self, monkeypatch):
        """Passthrough when LLM response has no shortened forms."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content="Plain response with no URLs")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send("test")
        assert conv2.content == "Plain response with no URLs"

    @pytest.mark.asyncio
    async def test_no_restore_when_substitutor_disabled(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        assert conv.enable_substitutor is False

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content="Some~shortened~form")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send("test")
        assert conv2.content == "Some~shortened~form"

    @pytest.mark.asyncio
    async def test_empty_response_passthrough(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL)

        async def fake_generate(request: LLMRequest):
            _ = request
            return create_test_model_response(content="")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv2 = await conv.send("test")
        assert conv2.content == ""


class TestEagerRestoreSendStructured:
    """Tests for eager restore in Conversation.send_structured()."""

    @pytest.mark.asyncio
    async def test_structured_content_and_parsed_restored(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL, LONG_URL_2)
        short_1 = shorts[LONG_URL]
        short_2 = shorts[LONG_URL_2]

        parsed = ItemList(items=[short_1, short_2], summary=f"See {short_1}")
        json_content = parsed.model_dump_json()

        async def fake_generate_structured(request: LLMRequest):
            assert request.response.format is ItemList
            return create_test_structured_model_response(parsed=parsed, content=json_content)

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate_structured)

        conv2 = await conv.send_structured(f"Check: {LONG_URL} {LONG_URL_2}", ItemList)

        # Content JSON is restored
        assert LONG_URL in conv2.content
        assert short_1 not in conv2.content

        # Parsed model fields are restored
        assert conv2.parsed is not None
        assert conv2.parsed.items[0] == LONG_URL
        assert conv2.parsed.items[1] == LONG_URL_2
        assert LONG_URL in conv2.parsed.summary

    @pytest.mark.asyncio
    async def test_structured_no_restore_when_no_patterns(self, monkeypatch):
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        parsed = ItemList(items=["plain"], summary="no urls")

        async def fake_generate_structured(request: LLMRequest):
            assert request.response.format is ItemList
            return create_test_structured_model_response(parsed=parsed)

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate_structured)

        conv2 = await conv.send_structured("test", ItemList)
        assert conv2.parsed is not None
        assert conv2.parsed.items == ["plain"]


class TestMultiTurnReShortening:
    """Tests for multi-turn conversations with eager restore + re-shortening."""

    @pytest.mark.asyncio
    async def test_second_turn_sends_shortened_history(self, monkeypatch):
        """Verify that restored content is re-shortened when sent to LLM on next turn."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL)
        short = shorts[LONG_URL]

        captured_messages: list[list[CoreMessage]] = []

        async def fake_generate(request: LLMRequest):
            captured_messages.append(list(request.messages))
            if len(captured_messages) == 1:
                return create_test_model_response(content=f"Found: {short}")
            return create_test_model_response(content="OK")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        # Turn 1
        conv2 = await conv.send(f"Check this: {LONG_URL}")
        assert conv2.content == f"Found: {LONG_URL}"

        # Turn 2
        await conv2.send("follow up")

        # Verify turn 2 messages: assistant history should be re-shortened
        turn2_messages = captured_messages[1]
        assistant_msgs = [m for m in turn2_messages if m.role == Role.ASSISTANT]
        assert len(assistant_msgs) >= 1
        # The assistant message should contain the shortened form, not the long URL
        assert any(short in (m.content if isinstance(m.content, str) else "") for m in assistant_msgs)

    @pytest.mark.asyncio
    async def test_three_turn_conversation(self, monkeypatch):
        """Verify 3-turn conversation: content always restored, history always shortened."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        shorts = _get_shortened_forms(LONG_URL)
        short = shorts[LONG_URL]

        call_count = 0

        async def fake_generate(request: LLMRequest):
            _ = request
            nonlocal call_count
            call_count += 1
            return create_test_model_response(content=f"Turn {call_count}: {short}")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = await conv.send(f"Check: {LONG_URL}")
        assert conv.content == f"Turn 1: {LONG_URL}"

        conv = await conv.send("msg 2")
        assert conv.content == f"Turn 2: {LONG_URL}"

        conv = await conv.send("msg 3")
        assert conv.content == f"Turn 3: {LONG_URL}"
        assert call_count == 3


class TestSubstitutorRoundTrip:
    """Tests for the substitute/restore round-trip invariants."""

    def test_substitute_idempotent_on_shortened_text(self):
        """substitute(already_shortened) should not change the text."""
        sub = URLSubstitutor()
        sub.prepare([LONG_URL])
        short = sub.get_mappings()[LONG_URL]

        text = f"See {short} for details"
        assert sub.substitute(text) == text

    def test_roundtrip_substitute_restore(self):
        """substitute(restore(shortened)) should reproduce the shortened text."""
        sub = URLSubstitutor()
        sub.prepare([LONG_URL])
        short = sub.get_mappings()[LONG_URL]

        text = f"Check {short}"
        restored = sub.restore(text)
        assert LONG_URL in restored

        re_shortened = sub.substitute(restored)
        assert re_shortened == text

    def test_roundtrip_multiple_patterns(self):
        """Round-trip with multiple URLs and tx hashes."""
        sub = URLSubstitutor()
        sub.prepare([LONG_URL, LONG_URL_2, TX_HASH])
        mappings = sub.get_mappings()

        text = " | ".join(mappings.values())
        restored = sub.restore(text)
        re_shortened = sub.substitute(restored)
        assert re_shortened == text

    def test_restore_then_substitute_preserves_surrounding_text(self):
        """Surrounding plain text is preserved through round-trip."""
        sub = URLSubstitutor()
        sub.prepare([LONG_URL])
        short = sub.get_mappings()[LONG_URL]

        text = f"Before {short} middle {short} after"
        restored = sub.restore(text)
        re_shortened = sub.substitute(restored)
        assert re_shortened == text

    def test_mixed_shortened_and_plain_urls(self):
        """Text with both shortened forms and plain short URLs."""
        sub = URLSubstitutor()
        sub.prepare([LONG_URL])
        short = sub.get_mappings()[LONG_URL]
        plain_url = "https://example.com"

        text = f"{short} and {plain_url}"
        restored = sub.restore(text)
        assert LONG_URL in restored
        assert plain_url in restored

        re_shortened = sub.substitute(restored)
        assert re_shortened == text
