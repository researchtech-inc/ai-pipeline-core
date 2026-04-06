"""Tests for span-based deployment database wiring."""

# pyright: reportPrivateUsage=false

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid7

import pytest
from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._helpers import _build_flow_cache_key, _compute_input_fingerprint
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask


class WireInputDoc(Document):
    pass


class WireMiddleDoc(Document):
    pass


class WireOutputDoc(Document):
    pass


class WireToMiddleTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[WireInputDoc, ...]) -> tuple[WireMiddleDoc, ...]:
        return (WireMiddleDoc.derive(derived_from=(input_docs[0],), name="middle.txt", content="m"),)


class WireToOutputTask(PipelineTask):
    @classmethod
    async def run(cls, middle_docs: tuple[WireMiddleDoc, ...]) -> tuple[WireOutputDoc, ...]:
        return (WireOutputDoc.derive(derived_from=(middle_docs[0],), name="output.txt", content="o"),)


class WireFailingTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, middle_docs: tuple[WireMiddleDoc, ...]) -> tuple[WireOutputDoc, ...]:
        _ = middle_docs
        raise RuntimeError("deliberate test failure")


class WireConversationTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[WireInputDoc, ...]) -> tuple[WireOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("hello", purpose="wire-llm")
        return (WireOutputDoc.derive(derived_from=(input_docs[0],), name="output.txt", content=conv.content),)


class WireFlowOne(PipelineFlow):
    async def run(self, input_docs: tuple[WireInputDoc, ...], options: FlowOptions) -> tuple[WireMiddleDoc, ...]:
        _ = options
        return await WireToMiddleTask.run(input_docs=input_docs)


class WireFlowTwo(PipelineFlow):
    async def run(self, middle_docs: tuple[WireMiddleDoc, ...], options: FlowOptions) -> tuple[WireOutputDoc, ...]:
        _ = options
        return await WireToOutputTask.run(middle_docs=middle_docs)


class WireFailingFlowTwo(PipelineFlow):
    async def run(self, middle_docs: tuple[WireMiddleDoc, ...], options: FlowOptions) -> tuple[WireOutputDoc, ...]:
        _ = options
        return await WireFailingTask.run(middle_docs=middle_docs)


class WireConversationFlow(PipelineFlow):
    async def run(self, input_docs: tuple[WireInputDoc, ...], options: FlowOptions) -> tuple[WireOutputDoc, ...]:
        _ = options
        return await WireConversationTask.run(input_docs=input_docs)


class WireResult(DeploymentResult):
    doc_count: int = 0


class WireTwoStageDeployment(PipelineDeployment[FlowOptions, WireResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> Sequence[PipelineFlow]:
        return [WireFlowOne(), WireFlowTwo()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> WireResult:
        return WireResult(success=True, doc_count=len(documents))


class WireFailingDeployment(PipelineDeployment[FlowOptions, WireResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> Sequence[PipelineFlow]:
        return [WireFlowOne(), WireFailingFlowTwo()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> WireResult:
        return WireResult(success=False)


class WireConversationDeployment(PipelineDeployment[FlowOptions, WireResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> Sequence[PipelineFlow]:
        return [WireConversationFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> WireResult:
        return WireResult(success=True, doc_count=len(documents))


def _make_input_doc() -> WireInputDoc:
    return WireInputDoc.create_root(name="input.txt", content="test", reason="wiring test")


def _make_response(
    *,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    cost: float = 0.0,
    model: str = "test-model",
) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
        cost=cost,
        model=model,
        response_id="resp-wire",
        metadata={"time_taken": 0.2, "first_token_time": 0.1},
        citations=(),
        reasoning_content="reasoning",
        tool_calls=(),
    )


def _make_fake_generate(result: ModelResponse[str]):
    async def _fake_generate(*args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        return result

    return _fake_generate


async def _run_with_db(
    deployment: PipelineDeployment[Any, Any],
    database: _MemoryDatabase,
    *,
    run_id: str = "wire-test",
    docs: list[Document] | None = None,
    deployment_span_id: UUID | None = None,
    root_deployment_id: UUID | None = None,
) -> Any:
    if docs is None:
        docs = [_make_input_doc()]
    return await deployment._run_with_context(
        run_id,
        docs,
        FlowOptions(),
        deployment_span_id=deployment_span_id,
        root_deployment_id=root_deployment_id,
        database=database,
    )


def _spans(database: _MemoryDatabase, kind: str) -> list[Any]:
    return sorted((span for span in database._spans.values() if span.kind == kind), key=lambda span: span.sequence_no)


@pytest.fixture
def db() -> _MemoryDatabase:
    return _MemoryDatabase()


class TestDeploymentSpans:
    def test_build_flow_cache_key_matches_redesign_format(self) -> None:
        key = _build_flow_cache_key(
            input_fingerprint="a1b2c3d4e5f6g7h8",
            flow_class=WireFlowOne,
            step=2,
            flow_params={},
        )

        assert key == f"flow:a1b2c3d4e5f6g7h8:{WireFlowOne.__module__}:{WireFlowOne.__qualname__}:2"

    def test_deployment_span_created(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        asyncio.run(_run_with_db(deployment, db))

        deployment_spans = _spans(db, SpanKind.DEPLOYMENT)
        assert len(deployment_spans) == 1
        deployment_span = deployment_spans[0]
        assert deployment_span.status == SpanStatus.COMPLETED
        assert deployment_span.deployment_id == deployment_span.span_id
        output_payload = json.loads(deployment_span.output_json)
        meta = json.loads(deployment_span.meta_json)
        assert output_payload["result"]["data"]["success"] is True
        assert meta["input_fingerprint"]
        assert len(_spans(db, SpanKind.FLOW)) == 2

    def test_root_input_documents_persist_content_addressed(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        asyncio.run(_run_with_db(deployment, db))

        assert len(db._documents) >= 1
        input_doc = next(document for document in db._documents.values() if document.document_type == "WireInputDoc")
        assert input_doc.name == "input.txt"

    def test_flow_spans_are_children_of_deployment(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        asyncio.run(_run_with_db(deployment, db))

        deployment_span = _spans(db, SpanKind.DEPLOYMENT)[0]
        flow_spans = _spans(db, SpanKind.FLOW)
        assert [span.sequence_no for span in flow_spans] == [0, 1]
        assert all(span.parent_span_id == deployment_span.span_id for span in flow_spans)
        assert all(span.status == SpanStatus.COMPLETED for span in flow_spans)

    def test_completed_flow_spans_persist_deterministic_cache_keys(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        input_doc = _make_input_doc()
        asyncio.run(_run_with_db(deployment, db, docs=[input_doc]))

        fingerprint = _compute_input_fingerprint([input_doc], FlowOptions())
        flow_spans = _spans(db, SpanKind.FLOW)

        assert [span.cache_key for span in flow_spans] == [
            _build_flow_cache_key(input_fingerprint=fingerprint, flow_class=WireFlowOne, step=1, flow_params={}),
            _build_flow_cache_key(input_fingerprint=fingerprint, flow_class=WireFlowTwo, step=2, flow_params={}),
        ]

    def test_cached_flow_spans_keep_cache_key(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        input_doc = _make_input_doc()
        asyncio.run(_run_with_db(deployment, db, docs=[input_doc], run_id="first-run"))
        asyncio.run(_run_with_db(deployment, db, docs=[input_doc], run_id="second-run"))

        fingerprint = _compute_input_fingerprint([input_doc], FlowOptions())
        cached_flow_spans = sorted(
            (span for span in db._spans.values() if span.kind == SpanKind.FLOW and span.status == SpanStatus.CACHED),
            key=lambda span: span.sequence_no,
        )

        assert [span.cache_key for span in cached_flow_spans] == [
            _build_flow_cache_key(input_fingerprint=fingerprint, flow_class=WireFlowOne, step=1, flow_params={}),
            _build_flow_cache_key(input_fingerprint=fingerprint, flow_class=WireFlowTwo, step=2, flow_params={}),
        ]

    def test_flow_spans_keep_zero_cost_rollup(self, db: _MemoryDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
        deployment = WireConversationDeployment()
        response = _make_response(content="hello", prompt_tokens=10, completion_tokens=4, cached_tokens=2, cost=0.42)
        monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", _make_fake_generate(response))

        asyncio.run(_run_with_db(deployment, db))

        deployment_span = _spans(db, SpanKind.DEPLOYMENT)[0]
        flow_span = _spans(db, SpanKind.FLOW)[0]
        llm_round = _spans(db, SpanKind.LLM_ROUND)[0]
        assert deployment_span.cost_usd == 0.0
        assert flow_span.cost_usd == 0.0
        assert llm_round.cost_usd == pytest.approx(0.42)
        totals = asyncio.run(db.get_deployment_cost_totals(deployment_span.root_deployment_id))
        assert totals.cost_usd == pytest.approx(0.42)

    def test_failing_flow_marks_flow_and_deployment_failed(self, db: _MemoryDatabase) -> None:
        deployment = WireFailingDeployment()
        with pytest.raises(RuntimeError, match="deliberate test failure"):
            asyncio.run(_run_with_db(deployment, db))

        deployment_span = _spans(db, SpanKind.DEPLOYMENT)[0]
        flow_spans = _spans(db, SpanKind.FLOW)
        assert deployment_span.status == SpanStatus.FAILED
        assert flow_spans[0].status == SpanStatus.COMPLETED
        assert flow_spans[1].status == SpanStatus.FAILED
        assert flow_spans[1].error_type == "RuntimeError"

    def test_run_with_context_accepts_preallocated_ids(self, db: _MemoryDatabase) -> None:
        deployment = WireTwoStageDeployment()
        deployment_span_id = uuid7()
        root_deployment_id = uuid7()
        asyncio.run(
            _run_with_db(
                deployment,
                db,
                deployment_span_id=deployment_span_id,
                root_deployment_id=root_deployment_id,
            )
        )

        deployment_span = _spans(db, SpanKind.DEPLOYMENT)[0]
        assert deployment_span.span_id == deployment_span_id
        assert deployment_span.deployment_id == deployment_span_id
        assert deployment_span.root_deployment_id == root_deployment_id
