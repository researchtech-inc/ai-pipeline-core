"""Span-era integration tests for deployment lifecycle and cache behavior."""

# pyright: reportPrivateUsage=false

import json
from collections.abc import Generator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import Conversation, DeploymentResult, Document, FlowOptions, PipelineDeployment, Tool, traced_operation
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import RawToolCall, TokenUsage
from ai_pipeline_core.database import CostTotals, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._helpers import _build_flow_cache_key, _compute_input_fingerprint
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask
from tests.llm.conftest import make_tool_call


@pytest.fixture(autouse=True)
def _suppress_registration() -> Generator[None]:
    return


class LifecycleInputDocument(Document):
    """Input document for lifecycle integration tests."""


class LifecycleOutputDocument(Document):
    """Output document for lifecycle integration tests."""


class LifecycleSearchTool(Tool):
    """Tool used to force a traced tool loop inside the deployment lifecycle."""

    class Input(BaseModel):
        query: str = Field(description="Search query.")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        return self.Output(result=f"Search result for {input.query}")


class LifecycleTask(PipelineTask):
    """Task that emits task, operation, conversation, llm_round, and tool_call spans."""

    @classmethod
    async def run(cls, input_docs: tuple[LifecycleInputDocument, ...]) -> tuple[LifecycleOutputDocument, ...]:
        async with traced_operation("prepare-answer", description="integration traced operation"):
            conv = Conversation(model="test-model", enable_substitutor=False, include_date=False)
            conv = await conv.send(
                "Use the tool to answer the question.",
                purpose="integration-lifecycle",
                tools=[LifecycleSearchTool()],
            )
        return (
            LifecycleOutputDocument.derive(
                derived_from=(input_docs[0],),
                name="answer.txt",
                content=conv.content,
                description="Lifecycle integration output",
            ),
        )


class LifecycleFlow(PipelineFlow):
    """Single-flow deployment used for end-to-end lifecycle assertions."""

    async def run(self, input_docs: tuple[LifecycleInputDocument, ...], options: FlowOptions) -> tuple[LifecycleOutputDocument, ...]:
        _ = options
        return await LifecycleTask.run(input_docs=input_docs)


class LifecycleResult(DeploymentResult):
    output_count: int = 0


class LifecycleDeployment(PipelineDeployment[FlowOptions, LifecycleResult]):
    """Deployment used to validate the full span-based lifecycle."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [LifecycleFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> LifecycleResult:
        _ = (run_id, options)
        return LifecycleResult(success=True, output_count=len(documents))


class CachedLifecycleDeployment(LifecycleDeployment):
    """Same lifecycle deployment with cross-run flow caching enabled."""

    cache_ttl = timedelta(hours=24)


def _make_response(
    *,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    cost: float = 0.0,
    response_id: str = "resp-integration",
    tool_calls: tuple[RawToolCall, ...] = (),
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
        model="test-model",
        response_id=response_id,
        metadata={"time_taken": 0.2, "first_token_time": 0.1},
        citations=(),
        reasoning_content="reasoning",
        tool_calls=tool_calls,
    )


class _GenerateSequence:
    def __init__(self, responses: Sequence[ModelResponse[str] | BaseException]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def __call__(self, *args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        if not self._responses:
            raise AssertionError("core_generate was called more times than expected in the integration test.")
        self.call_count += 1
        next_item = self._responses.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item


async def _run_deployment(
    deployment: PipelineDeployment[Any, Any],
    database: _MemoryDatabase,
    *,
    run_id: str,
    documents: Sequence[Document] | None = None,
) -> Any:
    resolved_documents = (
        list(documents)
        if documents is not None
        else [
            LifecycleInputDocument.create_root(
                name="input.txt",
                content="seed",
                reason="integration",
            )
        ]
    )
    return await deployment._run_with_context(run_id, resolved_documents, FlowOptions(), database=database)


def _sorted_spans(database: _MemoryDatabase, kind: str, *, status: str | None = None) -> list[SpanRecord]:
    spans = [span for span in database._spans.values() if span.kind == kind]
    if status is not None:
        spans = [span for span in spans if span.status == status]
    return sorted(spans, key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)))


def _token_totals(llm_round_spans: Sequence[SpanRecord]) -> CostTotals:
    totals = CostTotals()
    for span in llm_round_spans:
        detail = json.loads(span.metrics_json)
        totals = CostTotals(
            cost_usd=totals.cost_usd + span.cost_usd,
            tokens_input=totals.tokens_input + int(detail.get("tokens_input", 0)),
            tokens_output=totals.tokens_output + int(detail.get("tokens_output", 0)),
            tokens_cache_read=totals.tokens_cache_read + int(detail.get("tokens_cache_read", 0)),
            tokens_reasoning=totals.tokens_reasoning + int(detail.get("tokens_reasoning", 0)),
        )
    return totals


@pytest.mark.asyncio
async def test_full_deployment_lifecycle_records_complete_span_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    generate = _GenerateSequence([
        _make_response(
            content="",
            prompt_tokens=10,
            completion_tokens=1,
            cached_tokens=3,
            reasoning_tokens=2,
            response_id="resp-round-1",
            tool_calls=(make_tool_call("tool-1", "lifecycle_search_tool", '{"query": "pipeline spans"}'),),
        ),
        _make_response(
            content="final integrated answer",
            prompt_tokens=16,
            completion_tokens=5,
            cached_tokens=4,
            reasoning_tokens=1,
            cost=0.42,
            response_id="resp-round-2",
        ),
    ])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", generate)

    database = _MemoryDatabase()
    input_doc = LifecycleInputDocument.create_root(name="input.txt", content="seed", reason="integration lifecycle")
    result = await _run_deployment(LifecycleDeployment(), database, run_id="integration-lifecycle", documents=[input_doc])

    assert result.success
    assert result.output_count >= 1

    deployment_span = _sorted_spans(database, SpanKind.DEPLOYMENT)[0]
    flow_span = _sorted_spans(database, SpanKind.FLOW)[0]
    task_span = _sorted_spans(database, SpanKind.TASK)[0]
    operation_span = _sorted_spans(database, SpanKind.OPERATION)[0]
    conversation_span = _sorted_spans(database, SpanKind.CONVERSATION)[0]
    llm_round_spans = _sorted_spans(database, SpanKind.LLM_ROUND)
    tool_call_span = _sorted_spans(database, SpanKind.TOOL_CALL)[0]
    tree = await database.get_deployment_tree(deployment_span.root_deployment_id)

    assert {span.kind for span in tree} == {
        SpanKind.DEPLOYMENT,
        SpanKind.FLOW,
        SpanKind.TASK,
        SpanKind.ATTEMPT,
        SpanKind.OPERATION,
        SpanKind.CONVERSATION,
        SpanKind.LLM_ROUND,
        SpanKind.TOOL_CALL,
    }
    attempt_spans = [span for span in tree if span.kind == SpanKind.ATTEMPT]
    # Always-emit: both the FLOW and the TASK are wrapped in ATTEMPT spans
    flow_attempt_span = next(s for s in attempt_spans if s.parent_span_id == flow_span.span_id)
    task_attempt_span = next(s for s in attempt_spans if s.parent_span_id == task_span.span_id)
    assert flow_span.parent_span_id == deployment_span.span_id
    # Tasks inside the flow parent to the flow's ATTEMPT span, not directly to the FLOW
    assert task_span.parent_span_id == flow_attempt_span.span_id
    assert operation_span.parent_span_id == task_attempt_span.span_id
    assert conversation_span.parent_span_id == operation_span.span_id
    assert [span.parent_span_id for span in llm_round_spans] == [conversation_span.span_id, conversation_span.span_id]
    assert tool_call_span.parent_span_id == conversation_span.span_id
    assert [span.kind for span in sorted(await database.get_child_spans(conversation_span.span_id), key=lambda span: span.sequence_no)] == [
        SpanKind.LLM_ROUND,
        SpanKind.TOOL_CALL,
        SpanKind.LLM_ROUND,
    ]

    totals = await database.get_deployment_cost_totals(deployment_span.root_deployment_id)
    expected_totals = _token_totals(llm_round_spans)
    assert totals == expected_totals
    assert all(span.cost_usd == 0.0 for span in tree if span.kind != SpanKind.LLM_ROUND)

    output_sha = task_span.output_document_shas[0]
    assert task_span.output_document_shas == (output_sha,)
    assert flow_span.output_document_shas == (output_sha,)
    assert deployment_span.output_document_shas == (output_sha,)
    persisted_output = await database.get_document(output_sha)
    assert persisted_output is not None
    assert persisted_output.document_type == "LifecycleOutputDocument"
    assert persisted_output.derived_from == (input_doc.sha256,)


@pytest.mark.asyncio
async def test_flow_cache_keys_hit_across_runs_with_new_root_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    generate = _GenerateSequence([
        _make_response(
            content="",
            prompt_tokens=9,
            completion_tokens=1,
            response_id="resp-cache-1",
            tool_calls=(make_tool_call("tool-cache", "lifecycle_search_tool", '{"query": "cache"}'),),
        ),
        _make_response(
            content="cached answer source",
            prompt_tokens=14,
            completion_tokens=4,
            cost=0.31,
            response_id="resp-cache-2",
        ),
    ])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", generate)

    database = _MemoryDatabase()
    input_doc = LifecycleInputDocument.create_root(name="input.txt", content="seed", reason="cache integration")
    deployment = CachedLifecycleDeployment()

    await _run_deployment(deployment, database, run_id="cache-run-1", documents=[input_doc])
    await _run_deployment(deployment, database, run_id="cache-run-2", documents=[input_doc])

    deployment_spans = _sorted_spans(database, SpanKind.DEPLOYMENT)
    completed_flow = _sorted_spans(database, SpanKind.FLOW, status=SpanStatus.COMPLETED)[0]
    cached_flow = _sorted_spans(database, SpanKind.FLOW, status=SpanStatus.CACHED)[0]
    fingerprint = _compute_input_fingerprint([input_doc], FlowOptions())
    expected_key = _build_flow_cache_key(input_fingerprint=fingerprint, flow_class=LifecycleFlow, step=1, flow_params={})

    assert len(deployment_spans) == 2
    assert deployment_spans[0].root_deployment_id != deployment_spans[1].root_deployment_id
    assert completed_flow.cache_key == expected_key
    assert cached_flow.cache_key == expected_key
    assert completed_flow.root_deployment_id != cached_flow.root_deployment_id
    assert cached_flow.output_document_shas == completed_flow.output_document_shas
    assert generate.call_count == 2
