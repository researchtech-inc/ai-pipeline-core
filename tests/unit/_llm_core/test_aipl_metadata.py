"""AIPL metadata and trace helper tests."""

import logging
from typing import Any

import httpx
import pytest

from ai_pipeline_core._llm_core import _aipl
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _attempt() -> AttemptRequest:
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
    )
    return AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call-123")


def test_build_aipl_metadata_includes_aipl_call_id() -> None:
    metadata = _aipl.build_aipl_metadata(_attempt())

    assert metadata == {"aipl": "1", "aipl_call_id": "call-123"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_fetch_trace_logs_non_200(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
) -> None:
    class TraceClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> TraceClient:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            _ = (exc_type, exc, traceback)

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            _ = headers
            return httpx.Response(status_code, request=httpx.Request("GET", url))

    from ai_pipeline_core._llm_core._config import LLMCoreConfig, override_config

    monkeypatch.setattr(_aipl.httpx, "AsyncClient", TraceClient)
    config = LLMCoreConfig(openai_api_key="key", openai_base_url="http://proxy/v1")
    with override_config(config), caplog.at_level(logging.DEBUG, logger="ai_pipeline_core._llm_core._aipl"):
        trace = await _aipl.fetch_trace("call-123")

    assert trace is None
    assert f"status={status_code}" in caplog.text
    assert "http://proxy/aipl/trace/call-123" in caplog.text
