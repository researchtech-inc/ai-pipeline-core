"""Tests for runtime execution guards and frozen document type metadata."""

# pyright: reportPrivateUsage=false

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core import Conversation, Document, FlowOptions
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask, pipeline_test_context
from ai_pipeline_core.pipeline._execution_context import FlowFrame, TaskContext, set_execution_context, set_task_context
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class _GuardInDoc(Document):
    """Input document for runtime guard tests."""


class _GuardOutDoc(Document):
    """Output document for runtime guard tests."""


class _InnerTask(PipelineTask):
    """Inner task used to prove nested task dispatch is rejected."""

    @classmethod
    async def run(cls, doc: _GuardInDoc) -> _GuardOutDoc:
        return _GuardOutDoc.derive(derived_from=(doc,), name="inner.txt", content="inner")


class _OuterTask(PipelineTask):
    """Outer task that illegally calls another task."""

    @classmethod
    async def run(cls, doc: _GuardInDoc) -> _GuardOutDoc:
        return await _InnerTask.run(doc)


class _GuardFlow(PipelineFlow):
    """Simple flow used for frozen metadata assertions."""

    async def run(self, docs: tuple[_GuardInDoc, ...], options: FlowOptions) -> tuple[_GuardOutDoc, ...]:
        _ = options
        return await _InnerTask.run(docs[0])


def _make_flow_frame() -> FlowFrame:
    return FlowFrame(
        name="guard-flow",
        flow_class_name="_GuardFlow",
        step=1,
        total_steps=1,
        flow_minutes=(1.0,),
        completed_minutes=0.0,
        flow_params={},
    )


class TestTaskInTaskGuard:
    @pytest.mark.asyncio
    async def test_task_calling_task_raises(self) -> None:
        inp = _GuardInDoc.create_root(name="guard.txt", content="x", reason="runtime-guard-test")
        with pipeline_test_context():
            with pytest.raises(RuntimeError, match="cannot be called from inside another task"):
                await _OuterTask.run(inp)


class TestConversationInFlowGuard:
    @pytest.mark.asyncio
    async def test_conversation_send_from_flow_scope_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        @asynccontextmanager
        async def _unexpected_open_stream(
            req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
        ) -> AsyncIterator[Any]:
            _ = messages, api_kwargs
            if req is not None:
                raise AssertionError("Conversation-in-flow guard should fire before transport opens.")
            yield None

        monkeypatch.setattr(_transport, "open_stream", _unexpected_open_stream)
        with pipeline_test_context() as ctx:
            flow_ctx = ctx.with_flow(_make_flow_frame())
            with (
                set_execution_context(flow_ctx),
                set_task_context(TaskContext(scope_kind="flow", task_class_name="_GuardFlow")),
            ):
                with pytest.raises(RuntimeError, match="LLM calls must happen inside a PipelineTask"):
                    conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
                    await conv.send("hello")


class TestFrozenDocumentTypes:
    def test_task_document_types_are_frozen_tuples(self) -> None:
        assert _InnerTask.input_document_types == (_GuardInDoc,)
        assert _InnerTask.output_document_types == (_GuardOutDoc,)
        with pytest.raises(TypeError, match="frozen after class definition"):
            _InnerTask.input_document_types = ()

    def test_flow_document_types_are_frozen_tuples(self) -> None:
        assert _GuardFlow.input_document_types == (_GuardInDoc,)
        assert _GuardFlow.output_document_types == (_GuardOutDoc,)
        with pytest.raises(TypeError, match="frozen after class definition"):
            _GuardFlow.output_document_types = ()
