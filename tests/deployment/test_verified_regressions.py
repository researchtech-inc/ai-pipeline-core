"""Regression tests for verified deployment bugs that are not yet covered."""

from __future__ import annotations

import socket
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._deployment_runtime import _reuse_cached_flow_output
from ai_pipeline_core.deployment._helpers import extract_generic_params
from ai_pipeline_core.deployment._resolve import _is_private_ip
from ai_pipeline_core.deployment.base import DeploymentResult, PipelineDeployment
from ai_pipeline_core.deployment.remote import _poll_remote_flow_run
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import FlowOptions, PipelineFlow


class _CacheInputDoc(Document):
    """Input document for cache regression tests."""


class _CacheOutputDoc(Document):
    """Output document for cache regression tests."""


class _CacheFlow(PipelineFlow):
    """Flow used for cache regression tests."""

    async def run(
        self,
        documents: tuple[_CacheInputDoc, ...],
        options: FlowOptions,
    ) -> tuple[_CacheOutputDoc, ...]:
        return ()


def _build_output_doc(name: str, content: str) -> _CacheOutputDoc:
    return _CacheOutputDoc.create_root(name=name, content=content, reason="test")


class _NeverFinalState:
    def is_final(self) -> bool:
        return False


class _NeverFinalClient:
    async def read_flow_run(self, flow_run_id):
        return SimpleNamespace(state=_NeverFinalState())


class _InheritanceOptions(FlowOptions):
    """Deployment options used for generic inheritance tests."""


class _InheritanceResult(DeploymentResult):
    """Deployment result used for generic inheritance tests."""


@pytest.mark.asyncio
async def test_cached_flow_requires_complete_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _MemoryDatabase()
    first = _build_output_doc("first.txt", "1")
    second = _build_output_doc("second.txt", "2")

    async def _partial_loader(*args, **kwargs):
        return [first]

    monkeypatch.setattr("ai_pipeline_core.deployment._deployment_runtime.load_documents_from_database", _partial_loader)
    cached_span = SimpleNamespace(output_document_shas=(first.sha256, second.sha256))
    monkeypatch.setattr(db, "get_cached_completion", AsyncMock(return_value=cached_span))

    result = await _reuse_cached_flow_output(
        database=db,
        cache_ttl=timedelta(hours=1),
        flow_cache_key="flow:test-cache",
        flow_class=_CacheFlow,
        flow_name="CacheFlow",
        step=1,
        total_steps=1,
        accumulated_docs=[],
    )

    assert result is None


@pytest.mark.asyncio
async def test_cached_flow_preserves_original_output_order(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _MemoryDatabase()
    first = _build_output_doc("first.txt", "1")
    second = _build_output_doc("second.txt", "2")

    async def _reversed_loader(*args, **kwargs):
        return [second, first]

    monkeypatch.setattr("ai_pipeline_core.deployment._deployment_runtime.load_documents_from_database", _reversed_loader)
    cached_span = SimpleNamespace(output_document_shas=(first.sha256, second.sha256))
    monkeypatch.setattr(db, "get_cached_completion", AsyncMock(return_value=cached_span))

    result = await _reuse_cached_flow_output(
        database=db,
        cache_ttl=timedelta(hours=1),
        flow_cache_key="flow:test-order",
        flow_class=_CacheFlow,
        flow_name="CacheFlow",
        step=1,
        total_steps=1,
        accumulated_docs=[],
    )

    assert result is not None
    _cached_span, previous_output_documents, _updated_docs = result
    assert tuple(doc.sha256 for doc in previous_output_documents) == (first.sha256, second.sha256)


@pytest.mark.asyncio
async def test_remote_poll_enforces_internal_deadline() -> None:
    from ai_pipeline_core.deployment.remote import RemoteDeploymentPollingError

    with pytest.raises(RemoteDeploymentPollingError, match=r"exceeded 0\.05s"):
        await _poll_remote_flow_run(
            _NeverFinalClient(),
            uuid4(),
            poll_interval=0.01,
            max_poll_seconds=0.05,
        )


def test_extract_generic_params_walks_mro() -> None:
    class _BaseDeployment(PipelineDeployment[_InheritanceOptions, _InheritanceResult]):
        def build_flows(self, options: _InheritanceOptions) -> list[PipelineFlow]:
            return [_CacheFlow()]

        @staticmethod
        def build_result(
            run_id: str,
            documents: tuple[Document, ...],
            options: _InheritanceOptions,
        ) -> _InheritanceResult:
            return _InheritanceResult(success=True)

    class _ChildDeployment(_BaseDeployment):
        pass

    assert extract_generic_params(_ChildDeployment, PipelineDeployment) == (
        _InheritanceOptions,
        _InheritanceResult,
    )


@pytest.mark.asyncio
async def test_is_private_ip_fails_closed_on_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_dns(*args, **kwargs):
        raise socket.gaierror("dns failed")

    monkeypatch.setattr(socket, "getaddrinfo", _raise_dns)
    assert await _is_private_ip("example.com") is True
