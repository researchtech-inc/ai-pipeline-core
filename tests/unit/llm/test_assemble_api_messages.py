"""Tests for ``assemble_api_messages`` — the shared message-assembly helper.

This helper is the cross-surface invariant: ``Conversation.send*`` and
``PromptContract.execute`` both route through it. Two callers passing
identical inputs MUST produce identical messages so the engine's
``prompt_cache_key`` matches across the two surfaces.
"""

import pytest

from ai_pipeline_core._llm_core import Role
from ai_pipeline_core._llm_core._transport import compute_cache_key, core_messages_to_api
from ai_pipeline_core._llm_core.types import CoreMessage, TextContent
from ai_pipeline_core.llm._conversation_messages import UserMessage
from ai_pipeline_core.llm._conversation_runtime import assemble_api_messages, prepare_substitutor
from tests.support.helpers import ConcreteDocument
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _cache_prefix_key(messages: list[dict[str, object]], context_count: int) -> str:
    """Compute the engine's prompt_cache_key over the first ``context_count`` messages."""
    api = core_messages_to_api(messages, max_inline_file_total_bytes=DEFAULT_TEST_MODEL.max_inline_file_total_bytes)
    return compute_cache_key(api[:context_count])


def _all_text(messages: list[CoreMessage]) -> str:
    """Flatten all text content from a list of CoreMessages."""
    chunks: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, tuple):
            chunks.extend(part.text for part in content if isinstance(part, TextContent))
    return "\n".join(chunks)


class TestCacheKeyParity:
    """Identical inputs must produce identical cache keys (cross-surface invariant)."""

    @pytest.mark.asyncio
    async def test_identical_inputs_produce_identical_cache_key(self):
        """Two calls with the same inputs hash the same prefix."""
        doc = ConcreteDocument.create_root(name="ctx.txt", content="static context body", reason="test")
        context = (doc,)
        messages = (UserMessage("hello"),)

        msgs_a, count_a = await assemble_api_messages(
            system_block=None,
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )
        msgs_b, count_b = await assemble_api_messages(
            system_block=None,
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )

        assert count_a == count_b == 1
        assert _cache_prefix_key(msgs_a, count_a) == _cache_prefix_key(msgs_b, count_b)

    @pytest.mark.asyncio
    async def test_identical_inputs_with_system_block_match(self):
        """System block + context: same inputs hash the same prefix on repeat."""
        doc = ConcreteDocument.create_root(name="ctx.txt", content="cached body", reason="test")
        context = (doc,)
        messages = (UserMessage("question"),)

        msgs_a, count_a = await assemble_api_messages(
            system_block="You are helpful.",
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )
        msgs_b, count_b = await assemble_api_messages(
            system_block="You are helpful.",
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )

        assert count_a == count_b == 2
        assert _cache_prefix_key(msgs_a, count_a) == _cache_prefix_key(msgs_b, count_b)


class TestCacheBoundaryShift:
    """When a SYSTEM message is prepended, the context count must bump by 1.

    The cacheable prefix's last message must remain the same context document
    in both cases; only the leading SYSTEM message is added.
    """

    @pytest.mark.asyncio
    async def test_system_prepend_bumps_count_by_one(self):
        """No-system count = N → with-system count = N+1, same context tail."""
        doc = ConcreteDocument.create_root(name="ctx.txt", content="cacheable context body", reason="test")
        context = (doc,)
        messages = (UserMessage("question"),)

        msgs_no_sys, count_no_sys = await assemble_api_messages(
            system_block=None,
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )
        msgs_with_sys, count_with_sys = await assemble_api_messages(
            system_block="You are helpful.",
            context=context,
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )

        assert count_no_sys == 1
        assert count_with_sys == 2
        assert msgs_with_sys[0].role == Role.SYSTEM
        assert msgs_with_sys[0].content == "You are helpful."
        # Last message of the cacheable prefix is the same context document in both cases.
        assert msgs_no_sys[count_no_sys - 1] == msgs_with_sys[count_with_sys - 1]

    @pytest.mark.asyncio
    async def test_no_context_no_shift(self):
        """Empty context: SYSTEM is prepended but the (zero) count does not bump."""
        msgs, count = await assemble_api_messages(
            system_block="System block.",
            context=(),
            messages=(UserMessage("hi"),),
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )
        assert count == 0
        assert msgs[0].role == Role.SYSTEM
        assert msgs[1].role == Role.USER

    @pytest.mark.asyncio
    async def test_multiple_context_messages_count_includes_all(self):
        """With multiple context docs, count covers all of them + 1 for SYSTEM."""
        doc1 = ConcreteDocument.create_root(name="a.txt", content="alpha body", reason="test")
        doc2 = ConcreteDocument.create_root(name="b.txt", content="bravo body", reason="test")
        msgs, count = await assemble_api_messages(
            system_block="Sys.",
            context=(doc1, doc2),
            messages=(UserMessage("q"),),
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )
        # 1 SYSTEM + 2 context docs in the cacheable prefix; 1 dynamic user message after.
        assert count == 3
        assert msgs[0].role == Role.SYSTEM
        assert msgs[count - 1].role == Role.USER  # last context document is a USER role
        assert msgs[count].role == Role.USER  # dynamic prompt


class TestEmptySystemBlock:
    """``system_block`` of ``None`` or ``""`` MUST produce the same output."""

    @pytest.mark.asyncio
    async def test_none_and_empty_string_equivalent(self):
        doc = ConcreteDocument.create_root(name="ctx.txt", content="ctx body", reason="test")
        context = (doc,)
        messages = (UserMessage("q"),)

        msgs_none, count_none = await assemble_api_messages(
            system_block=None, context=context, messages=messages, model=DEFAULT_TEST_MODEL, substitutor=None
        )
        msgs_empty, count_empty = await assemble_api_messages(
            system_block="", context=context, messages=messages, model=DEFAULT_TEST_MODEL, substitutor=None
        )

        assert msgs_none == msgs_empty
        assert count_none == count_empty == 1


class TestSubstitutorApplied:
    """A non-None substitutor with patterns rewrites text-bearing content."""

    @pytest.mark.asyncio
    async def test_long_url_shortened_in_output(self):
        """When the substitutor is prepared, the long URL is rewritten in core messages."""
        long_url = "https://example.com/docs/api/v2/reference/very/long/path/to/some/resource/file-here"
        doc = ConcreteDocument.create_root(name="links.txt", content=f"See {long_url} for details", reason="test")
        messages = (UserMessage(f"and check {long_url}"),)
        substitutor = prepare_substitutor(context=(doc,), messages=messages, enabled=True, model=DEFAULT_TEST_MODEL)
        assert substitutor is not None
        assert substitutor.pattern_count > 0

        msgs, count = await assemble_api_messages(
            system_block=None,
            context=(doc,),
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=substitutor,
        )

        assert count == 1
        assert long_url not in _all_text(msgs)

    @pytest.mark.asyncio
    async def test_none_substitutor_leaves_text_untouched(self):
        """substitutor=None: text passes through verbatim."""
        long_url = "https://example.com/docs/api/v2/reference/very/long/path/to/some/resource/file-here"
        doc = ConcreteDocument.create_root(name="links.txt", content=f"See {long_url} for details", reason="test")
        messages = (UserMessage(f"and check {long_url}"),)

        msgs, _ = await assemble_api_messages(
            system_block=None,
            context=(doc,),
            messages=messages,
            model=DEFAULT_TEST_MODEL,
            substitutor=None,
        )

        # Long URL appears verbatim in both context and the dynamic message.
        assert _all_text(msgs).count(long_url) >= 2
