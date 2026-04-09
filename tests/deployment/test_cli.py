"""Unit tests for ai_pipeline_core.deployment._cli — debug tracing and CLI wiring."""

# pyright: reportPrivateUsage=false

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_pipeline_core.deployment._types import ErrorCode, _NoopPublisher
from ai_pipeline_core.deployment._helpers import _classify_error, _create_publisher
from ai_pipeline_core.deployment.base import DeploymentResult, PipelineDeployment, _validate_flow_chain
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineFlow
from ai_pipeline_core.exceptions import LLMError, PipelineCoreError
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import Settings


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_llm_error(self):
        assert _classify_error(LLMError("fail")) == ErrorCode.PROVIDER_ERROR

    def test_cancelled(self):
        assert _classify_error(asyncio.CancelledError()) == ErrorCode.CANCELLED

    def test_timeout(self):
        assert _classify_error(TimeoutError()) == ErrorCode.DURATION_EXCEEDED

    def test_value_error(self):
        assert _classify_error(ValueError("bad")) == ErrorCode.INVALID_INPUT

    def test_type_error(self):
        assert _classify_error(TypeError("wrong type")) == ErrorCode.INVALID_INPUT

    def test_pipeline_error(self):
        assert _classify_error(PipelineCoreError("pipe")) == ErrorCode.PIPELINE_ERROR

    def test_unknown(self):
        assert _classify_error(RuntimeError("unknown")) == ErrorCode.UNKNOWN


# ---------------------------------------------------------------------------
# _create_publisher
# ---------------------------------------------------------------------------


class TestCreatePublisher:
    def test_noop_when_no_pubsub(self):
        s = Settings(pubsub_project_id="", pubsub_topic_id="")
        publisher = _create_publisher(s, "research")
        assert isinstance(publisher, _NoopPublisher)

    def test_noop_when_no_service_type(self):
        s = Settings(pubsub_project_id="proj", pubsub_topic_id="topic")
        publisher = _create_publisher(s, "")
        assert isinstance(publisher, _NoopPublisher)

    def test_pubsub_without_clickhouse_creates_publisher(self):
        """Pub/Sub no longer requires ClickHouse — PubSubPublisher is pure event transport."""
        s = Settings(
            pubsub_project_id="proj",
            pubsub_topic_id="topic",
            clickhouse_host="",
        )
        with patch("ai_pipeline_core.deployment._helpers.PubSubPublisher") as mock_cls:
            mock_cls.return_value = MagicMock()
            publisher = _create_publisher(s, "svc")
            mock_cls.assert_called_once()
            assert publisher is mock_cls.return_value


# ---------------------------------------------------------------------------
# _compute_input_fingerprint
# ---------------------------------------------------------------------------

from ai_pipeline_core.deployment._helpers import _compute_input_fingerprint, _heartbeat_loop


class ScopeDoc(Document):
    pass


class TestComputeInputFingerprint:
    def test_no_documents(self):
        opts = FlowOptions()
        fingerprint = _compute_input_fingerprint([], opts)
        assert len(fingerprint) == 16

    def test_with_documents(self):
        doc = ScopeDoc.create_root(name="test.txt", content="hello", reason="test")
        fingerprint = _compute_input_fingerprint([doc], FlowOptions())
        assert len(fingerprint) == 16

    def test_different_docs_different_scope(self):
        doc1 = ScopeDoc.create_root(name="a.txt", content="aaa", reason="test")
        doc2 = ScopeDoc.create_root(name="b.txt", content="bbb", reason="test")
        fingerprint1 = _compute_input_fingerprint([doc1], FlowOptions())
        fingerprint2 = _compute_input_fingerprint([doc2], FlowOptions())
        assert fingerprint1 != fingerprint2

    def test_same_docs_same_scope(self):
        doc = ScopeDoc.create_root(name="a.txt", content="aaa", reason="test")
        opts = FlowOptions()
        fingerprint1 = _compute_input_fingerprint([doc], opts)
        fingerprint2 = _compute_input_fingerprint([doc], opts)
        assert fingerprint1 == fingerprint2


# ---------------------------------------------------------------------------
# _validate_flow_chain
# ---------------------------------------------------------------------------


class FlowInputDoc(Document):
    pass


class FlowOutputDoc(Document):
    pass


class FlowOutputDoc2(Document):
    pass


class _DbFallbackInputDoc(Document):
    pass


class _DbFallbackOutputDoc(Document):
    pass


class _CliInputDoc(Document):
    pass


class _CliOutputDoc(Document):
    pass


class _UnrelatedInputDoc(Document):
    pass


class _DbFallbackFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_DbFallbackInputDoc, ...], options: FlowOptions) -> tuple[_DbFallbackOutputDoc, ...]:
        _ = (input_docs, options)
        return ()


class _Flow1(PipelineFlow):
    async def run(self, flow_inputs: tuple[FlowInputDoc, ...], options: FlowOptions) -> tuple[FlowOutputDoc, ...]:
        _ = (flow_inputs, options)
        return ()


class _Flow2(PipelineFlow):
    async def run(self, upstream_outputs: tuple[FlowOutputDoc, ...], options: FlowOptions) -> tuple[FlowOutputDoc2, ...]:
        _ = (upstream_outputs, options)
        return ()


class _Flow2Bad(PipelineFlow):
    """Flow whose input is not produced by _Flow1 — chain validation must reject this."""

    async def run(self, unrelated_input: _UnrelatedInputDoc, options: FlowOptions) -> tuple[FlowOutputDoc2, ...]:
        _ = (unrelated_input, options)
        return ()


class _DbFallbackResult(DeploymentResult):
    pass


class _DbFallbackDeployment(PipelineDeployment[FlowOptions, _DbFallbackResult]):
    flow_retries = 0
    name = "test-cli-db"
    options_type = FlowOptions
    result_type = _DbFallbackResult

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_DbFallbackFlow()]

    def build_result(self, options, documents, run_id, **kwargs) -> _DbFallbackResult:
        return _DbFallbackResult(success=True)


class _CliFlow(PipelineFlow):
    async def run(self, cli_inputs: tuple[_CliInputDoc, ...], options: FlowOptions) -> tuple[_CliOutputDoc, ...]:
        _ = (cli_inputs, options)
        return ()


class _CliResult(DeploymentResult):
    pass


class _CliDeployment(PipelineDeployment[FlowOptions, _CliResult]):
    flow_retries = 0
    name = "test-cli-order"
    options_type = FlowOptions
    result_type = _CliResult

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_CliFlow()]

    def build_result(self, options, documents, run_id, **kwargs) -> _CliResult:
        return _CliResult(success=True)


class TestValidateFlowChain:
    def test_single_flow_ok(self):
        _validate_flow_chain("CliPipeline", [_Flow1()])

    def test_chained_flows_ok(self):
        _validate_flow_chain("CliPipeline", [_Flow1(), _Flow2()])

    def test_unsatisfied_input_raises(self):
        with pytest.raises(TypeError, match="requires input types"):
            _validate_flow_chain("CliPipeline", [_Flow1(), _Flow2Bad()])


# ---------------------------------------------------------------------------
# _heartbeat_loop
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:
    async def test_heartbeat_publishes_and_can_cancel(self):
        publisher = MagicMock()
        publisher.publish_heartbeat = AsyncMock()
        with patch("ai_pipeline_core.deployment._helpers._HEARTBEAT_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(_heartbeat_loop(publisher, "run-1", root_deployment_id="root-1", span_id="span-1"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_heartbeat_swallows_exception(self):
        publisher = MagicMock()
        call_count = 0

        async def _failing_heartbeat(run_id: str, *, root_deployment_id: str, span_id: str) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("publish failed")

        publisher.publish_heartbeat = _failing_heartbeat

        with patch("ai_pipeline_core.deployment._helpers._HEARTBEAT_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(_heartbeat_loop(publisher, "run-1", root_deployment_id="root-1", span_id="span-1"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert call_count >= 1


# ---------------------------------------------------------------------------
# run_cli_for_deployment — database fallback
# ---------------------------------------------------------------------------


class TestCliDatabaseFallback:
    """When ClickHouse is not configured, CLI must use FilesystemDatabase in the working directory."""

    def test_no_clickhouse_uses_filesystem_database(self, tmp_path: Path):
        """run_cli_for_deployment must pass base_path=wd so FilesystemDatabase is used."""
        from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
        from ai_pipeline_core.deployment._cli import run_cli_for_deployment

        captured_database = None

        async def _capture_run(self, **kwargs):
            nonlocal captured_database
            captured_database = kwargs.get("database")
            return _DbFallbackResult(success=True)

        deployment = _DbFallbackDeployment()
        wd = tmp_path / "output"

        with (
            patch("sys.argv", ["test-cli-db", str(wd)]),
            patch.object(PipelineDeployment, "run", _capture_run),
            patch("ai_pipeline_core.deployment._cli.settings", Settings(clickhouse_host="")),
        ):
            run_cli_for_deployment(deployment)

        assert captured_database is not None, "deployment.run() was called without a database"
        assert isinstance(captured_database, FilesystemDatabase), (
            f"Expected FilesystemDatabase, got {type(captured_database).__name__}. "
            "CLI should fall back to FilesystemDatabase(wd) when ClickHouse is not configured."
        )
        assert captured_database._base_path == wd

    def test_cli_shuts_down_database_after_run(self, tmp_path: Path) -> None:
        from ai_pipeline_core.deployment._cli import run_cli_for_deployment

        database = MagicMock()
        events: list[str] = []

        async def _capture_run(self, **kwargs):
            return _CliResult(success=True)

        async def _capture_shutdown(database_arg):
            assert database_arg is database
            events.append("shutdown")

        deployment = _CliDeployment()
        wd = tmp_path / "output"

        with (
            patch("sys.argv", ["test-cli-order", str(wd)]),
            patch.object(PipelineDeployment, "run", _capture_run),
            patch("ai_pipeline_core.deployment._cli._create_span_database_from_settings", return_value=database),
            patch("ai_pipeline_core.deployment._cli._shutdown_database", _capture_shutdown),
            patch("ai_pipeline_core.deployment._cli.settings", Settings(clickhouse_host="")),
        ):
            run_cli_for_deployment(deployment)

        assert events == ["shutdown"]
