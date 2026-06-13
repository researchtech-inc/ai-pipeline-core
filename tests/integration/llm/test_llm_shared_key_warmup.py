"""Integration coverage for the shared prompt-cache-key warmup lane.

Exercises the deployed AIPL proxy: concurrent requests that share one cacheable
context elect a single warmup owner, and the followers are released against the
same deployment (HRW affinity on the static cache key). This validates the client
drives the shared-key warmup lane correctly and the proxy reports warmup
attribution back through transport metadata.

Scope note: this does NOT assert burst activation or the burst latch. Burst only
engages after ~30s of accumulated backlog (``burst_after_s``), which a small batch
cannot build, and the burst telemetry (``burst_used``/``effective_qps``/
``burst_latched``) lives only in the AIPL trace (``capture_trace``, not exposed via
``Conversation``). Burst/latch behavior is validated by the load benchmark, not here.
"""

import asyncio
from typing import Any

import pytest

from ai_pipeline_core.llm import AIModel, Conversation, ModelOptions
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument
from tests.support.model_catalog import ALTERNATE_WEAK_SCHEMA_TEST_MODEL

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]

_CONCURRENCY = 8


@pytest.fixture
def shared_context_doc(nonce: str) -> ConcreteDocument:
    """A >1024-token document so the request engages the explicit-key warmup path.

    The ``nonce`` keeps the cache key cold per run, so the batch deterministically
    elects one owner instead of riding a still-warm key from a previous run.
    """
    body = "AIPL shared-key warmup integration context. " * 400
    return ConcreteDocument.create_root(
        name="warmup-context.txt",
        content=f"{body}\nnonce={nonce}",
        reason="integration shared-key warmup context",
        description="Shared cacheable context for the warmup-lane test",
    )


class TestSharedKeyWarmup:
    """Live checks for the shared-cache-key warmup lane (owner election + follower release)."""

    @pytest.mark.asyncio
    async def test_shared_key_batch_elects_owner_and_releases_followers(
        self,
        shared_context_doc: ConcreteDocument,
        cost_budget: Any,
    ) -> None:
        """Concurrent shared-key requests: one warmup owner, followers on the same deployment, all succeed."""
        model: AIModel = ALTERNATE_WEAK_SCHEMA_TEST_MODEL  # mimo-v2.5

        async def one() -> Conversation[str]:
            return await Conversation(
                model=model,
                context=(shared_context_doc,),
                enable_substitutor=False,
                include_date=False,
                model_options=ModelOptions(reasoning_effort="low"),
            ).send(
                "Reply with exactly: warmup ok.",
                purpose="shared-key-warmup-integration",
            )

        results = await asyncio.gather(*(one() for _ in range(_CONCURRENCY)))

        for conv in results:
            cost_budget.add(conv)

        # Every concurrent request on the shared key succeeded against the live proxy.
        assert all(conv.content for conv in results)

        aipl = [conv._last_response.transport.aipl for conv in results if conv._last_response is not None]
        assert len(aipl) == _CONCURRENCY
        # A single static cache key pins HRW affinity to one deployment.
        assert {info.deployment_id for info in aipl} == {aipl[0].deployment_id}
        roles = [info.warmup.role for info in aipl]
        # At most one owner is elected for the shared key; the rest are released followers.
        assert roles.count("owner") <= 1
        assert any(role == "follower" for role in roles)
