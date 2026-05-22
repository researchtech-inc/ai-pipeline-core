"""Tests for disable_cache parameter in flow cache reuse."""

# pyright: reportPrivateUsage=false

from datetime import timedelta

import pytest

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._deployment_runtime import _reuse_cached_flow_output
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._flow import PipelineFlow
from ai_pipeline_core.pipeline.options import FlowOptions


class _CacheInputDoc(Document):
    """Input document type for cache testing."""


class _CacheOutputDoc(Document):
    """Output document type for cache testing."""


class _CacheTestFlow(PipelineFlow):
    """Flow for cache testing."""

    async def run(self, input_docs: tuple[_CacheInputDoc, ...], options: FlowOptions) -> tuple[_CacheOutputDoc, ...]:
        _ = options
        return (_CacheOutputDoc.derive(derived_from=input_docs, name="out.txt", content="cached"),)


class TestDisableCacheReuse:
    @pytest.mark.asyncio
    async def test_returns_none_when_disable_cache_true(self) -> None:
        db = _MemoryDatabase()
        result = await _reuse_cached_flow_output(
            database=db,
            cache_ttl=timedelta(hours=1),
            flow_cache_key="some-cache-key",
            flow_class=_CacheTestFlow,
            flow_name="_CacheTestFlow",
            step=1,
            total_steps=1,
            accumulated_docs=[],
            disable_cache=True,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_database(self) -> None:
        result = await _reuse_cached_flow_output(
            database=None,
            cache_ttl=timedelta(hours=1),
            flow_cache_key="key",
            flow_class=_CacheTestFlow,
            flow_name="_CacheTestFlow",
            step=1,
            total_steps=1,
            accumulated_docs=[],
            disable_cache=False,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_cache_ttl(self) -> None:
        db = _MemoryDatabase()
        result = await _reuse_cached_flow_output(
            database=db,
            cache_ttl=None,
            flow_cache_key="key",
            flow_class=_CacheTestFlow,
            flow_name="_CacheTestFlow",
            step=1,
            total_steps=1,
            accumulated_docs=[],
            disable_cache=False,
        )
        assert result is None
