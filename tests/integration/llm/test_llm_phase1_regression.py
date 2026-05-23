"""Integration tests for phase-1 ``_llm_core`` migration regressions.

Covers four behaviors introduced or refactored in phase 1 that are not
otherwise exercised by the existing integration suite:

1. Reasoning-carrier continuity across a tool round.
2. ``preferred_deployment_id`` soft affinity on multi-turn sends.
3. ``cache_ttl`` wire shape (``Ns``) + response cache hit on re-send.
4. Invalid image bytes are dropped with a WARNING through ``Conversation``.
"""

import logging
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.documents import Attachment
from ai_pipeline_core.llm import AIModel, Conversation, Tool
from ai_pipeline_core.settings import settings
from tests.support.helpers import (
    ConcreteDocument,
    TransportSpy,
    api_part_counts,
    captured_metadata,
    last_model_response,
)
from tests.support.model_catalog import DEFAULT_TEST_MODEL

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class CapitalLookup(Tool):
    """Look up the capital city of a country."""

    class Input(BaseModel):
        """Input for capital lookup."""

        country: str = Field(description="Country name to look up.")

    class Output(BaseModel):
        """Output from capital lookup."""

        capital: str = Field(description="Capital city name.")

    async def run(self, input: Input) -> Output:
        """Return the capital for a small set of known countries."""
        capitals = {"france": "Paris", "germany": "Berlin", "japan": "Tokyo"}
        return self.Output(capital=capitals.get(input.country.lower(), "Unknown"))


_KNOWN_REASONING_PSF_KEYS = (
    "reasoning_items",
    "reasoning_details",
    "thought_signature",
    "thought_signatures",
    "chain_of_thought",
)


def _api_message_carries_reasoning(message: dict[str, Any]) -> bool:
    """Return True when an API-shaped assistant message carries known reasoning state.

    Restricted to documented reasoning carrier keys so unrelated provider
    metadata cannot accidentally satisfy the assertion.
    """
    psf = message.get("provider_specific_fields")
    if isinstance(psf, dict) and any(psf.get(key) for key in _KNOWN_REASONING_PSF_KEYS):
        return True
    if message.get("thinking_blocks"):
        return True
    if message.get("reasoning_items"):
        return True
    for tool_call in message.get("tool_calls") or ():
        tc_psf = tool_call.get("provider_specific_fields") if isinstance(tool_call, dict) else None
        if isinstance(tc_psf, dict) and any(tc_psf.get(key) for key in _KNOWN_REASONING_PSF_KEYS):
            return True
    return False


def _model_response_has_known_carrier(response: ModelResponse[Any]) -> bool:
    """Return True when a ModelResponse carries known reasoning carrier keys."""
    psf = response.provider_specific_fields or {}
    if any(psf.get(key) for key in _KNOWN_REASONING_PSF_KEYS):
        return True
    if response.thinking_blocks:
        return True
    for tool_call in response.tool_calls or ():
        tc_psf = tool_call.provider_specific_fields or {}
        if any(tc_psf.get(key) for key in _KNOWN_REASONING_PSF_KEYS):
            return True
    return False


class TestReasoningContinuity:
    """Multi-turn tool-call payloads carry reasoning state when the model emits it.

    Deterministic ModelResponse -> CoreMessage -> API payload coverage lives
    in ``tests/unit/_llm_core/test_reasoning_signatures.py``. This integration
    test is a live smoke check that confirms the round-trip still functions
    against a real proxy: the second-turn payload exists, contains the
    assistant tool-call message, and — when the provider attached known
    reasoning carriers on turn 1 — those carriers survive into turn 2.
    """

    @pytest.mark.asyncio
    async def test_provider_carriers_survive_tool_round(
        self,
        test_model_choice: AIModel,
        transport_spy: TransportSpy,
        cost_budget: Any,
    ) -> None:
        """The assistant tool-call message must appear in turn 2 and preserve any carriers."""
        tool = CapitalLookup()
        conv = await Conversation(
            model=test_model_choice,
            enable_substitutor=False,
        ).send(
            "Use the capital_lookup tool to find the capital of France, then state it.",
            tools=[tool],
            tool_choice="required",
            max_tool_rounds=2,
            purpose=f"phase1-reasoning-continuity-{test_model_choice.name}",
        )
        cost_budget.add(conv)

        assert conv.tool_call_records, "expected at least one tool call"
        assert len(transport_spy.recorded_calls) >= 2, (
            f"expected >= 2 transport calls for the tool round, got {len(transport_spy.recorded_calls)}"
        )

        second_payload = transport_spy.recorded_calls[1].messages
        assistant_with_tools = next(
            (message for message in second_payload if message.get("role") == "assistant" and message.get("tool_calls")),
            None,
        )
        assert assistant_with_tools is not None, "expected an assistant tool-call message in the second-turn payload"

        # If turn-1 ModelResponse carried known reasoning state, the second
        # turn's assistant message must still carry it. If the provider did
        # not emit carriers, this branch is a no-op and the unit test covers
        # the deterministic round-trip.
        first_response = next(
            (message for message in conv.messages if isinstance(message, ModelResponse) and message.has_tool_calls),
            None,
        )
        if first_response is not None and _model_response_has_known_carrier(first_response):
            assert _api_message_carries_reasoning(assistant_with_tools), (
                f"turn 1 emitted known reasoning carriers, but turn 2's assistant tool-call "
                f"message did not preserve them; got keys: {sorted(assistant_with_tools.keys())}"
            )


class TestPreferredDeploymentAffinity:
    """Multi-turn sends must pin ``aipl_prefer_deployment_id`` to the prior deployment."""

    @pytest.mark.asyncio
    async def test_followup_carries_preferred_deployment_id(
        self,
        default_test_model: AIModel,
        nonce: str,
        transport_spy: TransportSpy,
        cost_budget: Any,
    ) -> None:
        """Turn 2's metadata must reference turn 1's deployment_id; turn 1 must not."""
        conv = await Conversation(
            model=default_test_model,
            enable_substitutor=False,
        ).send(
            f"Reply with exactly the word 'ack'. Nonce: {nonce}",
            purpose=f"phase1-prefer-deployment-1-{nonce}",
        )
        cost_budget.add(conv)

        first_deployment_id = last_model_response(conv).transport.aipl.deployment_id
        assert first_deployment_id, (
            "AIPL proxy is expected to return a deployment_id on the first turn; "
            "the preferred-deployment affinity hint cannot be verified without it."
        )

        conv = await conv.send(
            "Reply with exactly the word 'ack' again.",
            purpose=f"phase1-prefer-deployment-2-{nonce}",
        )
        cost_budget.add(conv)

        first_metadata = captured_metadata(transport_spy, index=0)
        assert "aipl_prefer_deployment_id" not in first_metadata, (
            f"turn 1 must not carry aipl_prefer_deployment_id; got {first_metadata.get('aipl_prefer_deployment_id')!r}"
        )

        second_metadata = captured_metadata(transport_spy, index=1)
        assert second_metadata.get("aipl_prefer_deployment_id") == first_deployment_id, (
            f"expected aipl_prefer_deployment_id={first_deployment_id!r} on turn 2, "
            f"got {second_metadata.get('aipl_prefer_deployment_id')!r}"
        )


class TestCacheTtlWireShape:
    """``cache_ttl=1`` translates to a 60s wire marker and produces a cache hit on re-send."""

    @pytest.mark.asyncio
    async def test_cache_marker_and_response_cache_hit(
        self,
        nonce: str,
        transport_spy: TransportSpy,
        cost_budget: Any,
    ) -> None:
        """First request carries cache_control={'type':'ephemeral','ttl':'60s'}; second is a cache hit."""
        model = AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=1)
        big_context = ConcreteDocument.create_root(
            name=f"large-context-{nonce}.txt",
            content=(
                f"Cache marker probe {nonce}\n\n"
                + "The following block is reference material that should be cached.\n"
                + ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 200)
            ),
            reason="cache marker integration probe",
        )

        async def _send_once() -> Conversation[str]:
            # The framework now auto-suppresses convenience SYSTEM injections
            # (date, substitutor guidance) when cache markers will be applied,
            # so the default ``include_date=True`` no longer breaks the Gemini
            # cached_content path. We pin both flags here to make the request
            # shape deterministic for the boundary assertion below.
            return await Conversation(
                model=model,
                context=(big_context,),
                enable_substitutor=False,
            ).send(
                "Reply with exactly the word 'ok'.",
                purpose=f"phase1-cache-ttl-{nonce}",
            )

        first = await _send_once()
        cost_budget.add(first)
        assert first.content

        first_messages = transport_spy.recorded_calls[0].messages
        expected = {"type": "ephemeral", "ttl": "60s"}

        # With ``cache_active`` suppressing the auto SYSTEM prompt, the
        # request is exactly two messages: the cacheable context document
        # (index 0) and the dynamic user prompt (index 1). The marker MUST
        # land on index 0 (the prefix boundary) and MUST NOT appear on
        # index 1.
        assert len(first_messages) == 2, (
            f"expected exactly [context, prompt] without an auto SYSTEM message, "
            f"got {len(first_messages)} messages: roles={[m.get('role') for m in first_messages]}"
        )
        boundary = first_messages[0]
        dynamic = first_messages[1]
        assert boundary.get("cache_control") == expected, (
            f"expected boundary message to carry cache_control={expected}; got {boundary.get('cache_control')!r}"
        )
        assert "cache_control" not in dynamic, (
            f"dynamic user prompt must not carry cache_control; got {dynamic.get('cache_control')!r}"
        )
        # The boundary's last content part also carries the marker (defense
        # in depth for providers that look at the part level instead of the
        # message level).
        boundary_content = boundary.get("content")
        if isinstance(boundary_content, list) and boundary_content:
            assert boundary_content[-1].get("cache_control") == expected, (
                f"expected boundary's last content part to carry cache_control={expected}; "
                f"got {boundary_content[-1].get('cache_control')!r}"
            )

        second = await _send_once()
        cost_budget.add(second)
        response = last_model_response(second)
        cache_hit = response.transport.aipl.response_cache_hit
        cached_tokens = response.usage.cached_tokens
        assert cache_hit or cached_tokens > 0, (
            f"expected response_cache_hit or cached_tokens > 0 on re-send; "
            f"got response_cache_hit={cache_hit}, cached_tokens={cached_tokens}"
        )


class TestFailureClassHeaderRoundTrip:
    """Successful proxy responses do not populate ``transport.aipl.failure_class``."""

    @pytest.mark.asyncio
    async def test_success_response_has_no_failure_class(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """The proxy must not stamp ``x-aipl-failure-class`` on healthy completions.

        The framework parses ``x-aipl-failure-class`` into ``AIPLInfo`` and
        routes terminal-for-model classes (``context_limit`` /
        ``capability_mismatch`` / ``refusal``) into ``AIModel`` fallback
        advancement; a spurious ``failure_class`` on success would advance
        the chain unnecessarily.
        """
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "Reply with exactly the word 'ok'.",
            purpose="phase2-failure-class-roundtrip",
        )
        cost_budget.add(conv)

        response = last_model_response(conv)
        aipl = response.transport.aipl
        assert aipl.failure_class is None, (
            f"expected no failure_class on success; got {aipl.failure_class!r}. "
            "If non-None, the proxy is stamping x-aipl-failure-class on healthy 200s."
        )
        assert aipl.explicit_cache_created is False
        assert aipl.explicit_cache_used is False


class TestInvalidImageDropAndWarn:
    """Random bytes labeled as an image attachment must be dropped with a WARNING."""

    @pytest.mark.asyncio
    async def test_invalid_image_attachment_dropped(
        self,
        default_test_model: AIModel,
        transport_spy: TransportSpy,
        cost_budget: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid image bytes log a 'validation failed' WARNING, are not forwarded,
        and the response still completes."""
        garbage = b"this is not a valid image " + b"\x00\x01\x02\x03" * 64
        doc = ConcreteDocument.create_root(
            name="report.md",
            content="# Report\n\nWith a corrupt screenshot attached.",
            attachments=(Attachment(name="screenshot.jpg", content=garbage),),
            reason="invalid image regression",
        )

        conv = Conversation(model=default_test_model, enable_substitutor=False).with_document(doc)
        with caplog.at_level(logging.WARNING):
            conv = await conv.send(
                "Reply with exactly the word 'ok'.",
                purpose="phase1-invalid-image",
            )

        cost_budget.add(conv)
        assert conv.content, "expected the conversation to complete after dropping the invalid image"

        validation_warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "validation failed" in record.getMessage()
        ]
        assert validation_warnings, (
            "expected at least one WARNING containing 'validation failed', "
            f"got: {[record.getMessage() for record in caplog.records]}"
        )

        first_payload = transport_spy.recorded_calls[0].messages
        assert api_part_counts(first_payload).get("image_url", 0) == 0, (
            "expected no image_url parts to be forwarded for the invalid image attachment"
        )
