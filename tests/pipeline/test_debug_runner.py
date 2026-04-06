"""Tests for run_task_debug and run_flow_debug debug execution helpers."""

# pyright: reportPrivateUsage=false

import pytest

from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._debug import DebugRunResult, run_flow_debug, run_task_debug
from ai_pipeline_core.pipeline._flow import PipelineFlow
from ai_pipeline_core.pipeline._task import PipelineTask
from ai_pipeline_core.pipeline.options import FlowOptions


# --- Module-level test documents/tasks/flows (required by _file_rules.py) ---


class _DebugInputDoc(Document):
    """Input document for debug runner tests."""


class _DebugOutputDoc(Document):
    """Output document for debug runner tests."""


class _SimpleTask(PipelineTask):
    """Task that derives an output document."""

    @classmethod
    async def run(cls, input_docs: tuple[_DebugInputDoc, ...]) -> tuple[_DebugOutputDoc, ...]:
        return tuple(_DebugOutputDoc.derive(name=f"out-{i}.md", content="result", derived_from=(doc,)) for i, doc in enumerate(input_docs))


class _FailingTask(PipelineTask):
    """Task that always fails."""

    @classmethod
    async def run(cls, input_docs: tuple[_DebugInputDoc, ...]) -> tuple[_DebugOutputDoc, ...]:
        _ = input_docs
        raise RuntimeError("intentional failure")


class _SimpleFlow(PipelineFlow):
    """Flow that runs _SimpleTask."""

    async def run(self, input_docs: tuple[_DebugInputDoc, ...], options: FlowOptions) -> tuple[_DebugOutputDoc, ...]:
        _ = options
        return await _SimpleTask.run(input_docs=input_docs)


def _make_input() -> _DebugInputDoc:
    return _DebugInputDoc.create_root(name="input.md", content="test content", reason="debug test")


class TestRunTaskDebug:
    @pytest.mark.asyncio
    async def test_returns_debug_result(self, tmp_path) -> None:
        result = await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "debug")
        assert isinstance(result, DebugRunResult)
        assert len(result.output_documents) == 1
        assert isinstance(result.output_documents[0], _DebugOutputDoc)
        assert result.output_dir == tmp_path / "debug"
        assert result.run_id.startswith("debug-")

    @pytest.mark.asyncio
    async def test_persists_spans(self, tmp_path) -> None:
        await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "debug")
        db = FilesystemDatabase(tmp_path / "debug", read_only=True)
        kinds = {span.kind for span in db._spans.values()}
        assert "deployment" in kinds
        assert "task" in kinds
        assert "attempt" in kinds

    @pytest.mark.asyncio
    async def test_persists_documents(self, tmp_path) -> None:
        result = await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "debug")
        db = FilesystemDatabase(tmp_path / "debug", read_only=True)
        assert result.output_documents[0].sha256 in db._documents

    @pytest.mark.asyncio
    async def test_custom_run_id(self, tmp_path) -> None:
        result = await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "debug", run_id="my-test-run")
        assert result.run_id == "my-test-run"

    @pytest.mark.asyncio
    async def test_flushes_on_failure(self, tmp_path) -> None:
        with pytest.raises(RuntimeError, match="intentional failure"):
            await run_task_debug(_FailingTask, (_make_input(),), output_dir=tmp_path / "debug")
        # Database should still be written (flushed in finally)
        assert (tmp_path / "debug").exists()

    @pytest.mark.asyncio
    async def test_with_external_database(self, tmp_path) -> None:
        db = FilesystemDatabase(tmp_path / "custom_db")
        result = await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "unused", database=db)
        assert len(result.output_documents) == 1
        # output_dir should point to database's actual path, not the unused one
        assert result.output_dir == tmp_path / "custom_db"
        # User-provided database is NOT auto-shutdown, so we can still use it
        assert any(span.kind == "task" for span in db._spans.values())
        await db.flush()
        await db.shutdown()


class _NoneReturningTask(PipelineTask):
    """Task that returns None."""

    @classmethod
    async def run(cls, input_docs: tuple[_DebugInputDoc, ...]) -> None:
        _ = input_docs
        return


class TestRunTaskDebugEdgeCases:
    @pytest.mark.asyncio
    async def test_none_return_produces_empty_tuple(self, tmp_path) -> None:
        result = await run_task_debug(_NoneReturningTask, (_make_input(),), output_dir=tmp_path / "debug")
        assert result.output_documents == ()

    @pytest.mark.asyncio
    async def test_auto_run_id_is_valid(self, tmp_path) -> None:
        from ai_pipeline_core.deployment._helpers import validate_run_id

        result = await run_task_debug(_SimpleTask, (_make_input(),), output_dir=tmp_path / "debug")
        validate_run_id(result.run_id)


class TestRunFlowDebug:
    @pytest.mark.asyncio
    async def test_returns_debug_result(self, tmp_path) -> None:
        result = await run_flow_debug(
            _SimpleFlow(),
            documents=(_make_input(),),
            options=FlowOptions(),
            output_dir=tmp_path / "debug",
        )
        assert isinstance(result, DebugRunResult)
        assert len(result.output_documents) >= 1
        assert isinstance(result.output_documents[0], _DebugOutputDoc)

    @pytest.mark.asyncio
    async def test_creates_flow_span_tree(self, tmp_path) -> None:
        await run_flow_debug(
            _SimpleFlow(),
            documents=(_make_input(),),
            options=FlowOptions(),
            output_dir=tmp_path / "debug",
        )
        db = FilesystemDatabase(tmp_path / "debug", read_only=True)
        kinds = {span.kind for span in db._spans.values()}
        assert "deployment" in kinds
        assert "flow" in kinds
        assert "task" in kinds
        assert "attempt" in kinds

    @pytest.mark.asyncio
    async def test_custom_run_id(self, tmp_path) -> None:
        result = await run_flow_debug(
            _SimpleFlow(),
            documents=(_make_input(),),
            options=FlowOptions(),
            output_dir=tmp_path / "debug",
            run_id="flow-test-run",
        )
        assert result.run_id == "flow-test-run"

    @pytest.mark.asyncio
    async def test_flow_auto_run_id_is_valid(self, tmp_path) -> None:
        from ai_pipeline_core.deployment._helpers import validate_run_id

        result = await run_flow_debug(
            _SimpleFlow(),
            documents=(_make_input(),),
            options=FlowOptions(),
            output_dir=tmp_path / "debug",
        )
        validate_run_id(result.run_id)
