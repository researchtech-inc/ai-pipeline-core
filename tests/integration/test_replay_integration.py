"""Span-era integration tests for download and replay."""

# pyright: reportPrivateUsage=false

import json
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import Conversation, DeploymentResult, Document, FlowOptions, PipelineDeployment, Tool
from ai_pipeline_core._llm_core import CoreMessage, TextContent
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import RawToolCall, TokenUsage
from ai_pipeline_core.database import SpanKind, SpanRecord
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.snapshot._download import download_deployment
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask
from ai_pipeline_core.replay import execute_span
from tests.llm.conftest import make_tool_call


@pytest.fixture(autouse=True)
def _suppress_registration() -> Generator[None]:
    return


class SnapshotInputDocument(Document):
    """Input document for snapshot replay integration tests."""


class SnapshotOutputDocument(Document):
    """Output document for snapshot replay integration tests."""


class HistoryInputDocument(Document):
    """Input document for conversation-history replay tests."""


class HistoryOutputDocument(Document):
    """Output document for conversation-history replay tests."""


class HistorySearchTool(Tool):
    """Tool used to create a replayable multi-turn conversation history."""

    class Input(BaseModel):
        query: str = Field(description="Search query.")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        return self.Output(result=f"Tool result for {input.query}")


class SnapshotTask(PipelineTask):
    """Task replayed from a downloaded span snapshot."""

    @classmethod
    async def run(cls, input_docs: tuple[SnapshotInputDocument, ...]) -> tuple[SnapshotOutputDocument, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False, include_date=False).with_context(input_docs[0])
        conv = await conv.send("Summarize the context document.", purpose="snapshot-task")
        return (
            SnapshotOutputDocument.derive(
                derived_from=(input_docs[0],),
                name="snapshot-output.txt",
                content=conv.content,
                description="snapshot replay output",
            ),
        )


class SnapshotFlow(PipelineFlow):
    async def run(self, input_docs: tuple[SnapshotInputDocument, ...], options: FlowOptions) -> tuple[SnapshotOutputDocument, ...]:
        _ = options
        return await SnapshotTask.run(input_docs=input_docs)


class SnapshotResult(DeploymentResult):
    output_count: int = 0


class SnapshotDeployment(PipelineDeployment[FlowOptions, SnapshotResult]):
    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [SnapshotFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> SnapshotResult:
        _ = (run_id, options)
        return SnapshotResult(success=True, output_count=len(documents))


class HistoryTask(PipelineTask):
    """Task that creates a tool loop followed by a normal follow-up turn."""

    @classmethod
    async def run(cls, input_docs: tuple[HistoryInputDocument, ...]) -> tuple[HistoryOutputDocument, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False, include_date=False)
        conv = await conv.send(
            "Search for the weather.",
            purpose="history-search",
            tools=[HistorySearchTool()],
        )
        conv = await conv.send("Give the concise answer.", purpose="history-followup")
        return (
            HistoryOutputDocument.derive(
                derived_from=(input_docs[0],),
                name="history-output.txt",
                content=conv.content,
                description="history replay output",
            ),
        )


class HistoryFlow(PipelineFlow):
    async def run(self, input_docs: tuple[HistoryInputDocument, ...], options: FlowOptions) -> tuple[HistoryOutputDocument, ...]:
        _ = options
        return await HistoryTask.run(input_docs=input_docs)


class HistoryResult(DeploymentResult):
    output_count: int = 0


class HistoryDeployment(PipelineDeployment[FlowOptions, HistoryResult]):
    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [HistoryFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> HistoryResult:
        _ = (run_id, options)
        return HistoryResult(success=True, output_count=len(documents))


def _make_response(
    *,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float = 0.0,
    response_id: str = "resp-replay",
    tool_calls: tuple[RawToolCall, ...] = (),
) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        cost=cost,
        model="test-model",
        response_id=response_id,
        metadata={"time_taken": 0.2, "first_token_time": 0.1},
        citations=(),
        reasoning_content="reasoning",
        tool_calls=tool_calls,
    )


class _GenerateRecorder:
    def __init__(self, responses: Sequence[ModelResponse[str] | BaseException]) -> None:
        self._responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: Sequence[CoreMessage], *args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        self.requests.append(_normalize_core_messages(messages))
        if not self._responses:
            raise AssertionError("core_generate was called more times than expected in the replay integration test.")
        next_item = self._responses.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item


async def _run_deployment(
    deployment: PipelineDeployment[Any, Any],
    database: _MemoryDatabase,
    *,
    run_id: str,
    documents: Sequence[Document],
) -> Any:
    return await deployment._run_with_context(run_id, list(documents), FlowOptions(), database=database)


def _normalize_core_messages(messages: Sequence[CoreMessage]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message.content, str):
            content: str | list[str] = message.content
        else:
            content = [part.text for part in message.content if isinstance(part, TextContent)]
        item: dict[str, Any] = {"role": message.role.value, "content": content}
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "function_name": tool_call.function_name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            ]
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.name:
            item["name"] = message.name
        normalized.append(item)
    return normalized


def _span_sort_key(span: SpanRecord) -> tuple[str, str]:
    return str(span.span_id), str(span.version)


@pytest.mark.asyncio
async def test_downloaded_snapshot_round_trips_spans_and_replays_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_generate = _GenerateRecorder([_make_response(content="stored summary", prompt_tokens=12, completion_tokens=4, cost=0.18)])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", original_generate)

    database = _MemoryDatabase()
    input_doc = SnapshotInputDocument.create_root(name="input.txt", content="snapshot source", reason="snapshot integration")
    await _run_deployment(SnapshotDeployment(), database, run_id="snapshot-run", documents=[input_doc])

    deployment_span = next(span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT)
    original_tree = sorted(await database.get_deployment_tree(deployment_span.root_deployment_id), key=_span_sort_key)

    bundle_dir = tmp_path / "snapshot-bundle"
    await download_deployment(database, deployment_span.root_deployment_id, bundle_dir)
    snapshot = FilesystemDatabase(bundle_dir)
    round_trip_tree = sorted(await snapshot.get_deployment_tree(deployment_span.root_deployment_id), key=_span_sort_key)

    assert round_trip_tree == original_tree

    task_span = next(span for span in round_trip_tree if span.kind == SpanKind.TASK)
    replay_generate = _GenerateRecorder([_make_response(content="replayed summary", prompt_tokens=12, completion_tokens=4, cost=0.18)])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", replay_generate)
    replayed = await execute_span(task_span.span_id, source_db=snapshot)

    assert len(replayed) == 1
    assert isinstance(replayed[0], SnapshotOutputDocument)
    assert replayed[0].content == b"replayed summary"
    assert replayed[0].derived_from == (input_doc.sha256,)


@pytest.mark.asyncio
async def test_replay_reconstructs_multiturn_history_with_tool_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_generate = _GenerateRecorder([
        _make_response(
            content="",
            prompt_tokens=8,
            completion_tokens=1,
            response_id="resp-history-1",
            tool_calls=(make_tool_call("history-tool-1", "history_search_tool", '{"query": "weather"}'),),
        ),
        _make_response(
            content="It is sunny today.",
            prompt_tokens=14,
            completion_tokens=4,
            response_id="resp-history-2",
            cost=0.21,
        ),
        _make_response(
            content="Concise: sunny.",
            prompt_tokens=18,
            completion_tokens=4,
            response_id="resp-history-3",
            cost=0.19,
        ),
    ])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", original_generate)

    database = _MemoryDatabase()
    input_doc = HistoryInputDocument.create_root(name="input.txt", content="history source", reason="history integration")
    await _run_deployment(HistoryDeployment(), database, run_id="history-run", documents=[input_doc])

    deployment_span = next(span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT)
    bundle_dir = tmp_path / "history-bundle"
    await download_deployment(database, deployment_span.root_deployment_id, bundle_dir)
    snapshot = FilesystemDatabase(bundle_dir)
    conversation_spans = sorted(
        [span for span in await snapshot.get_deployment_tree(deployment_span.root_deployment_id) if span.kind == SpanKind.CONVERSATION],
        key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)),
    )
    target_span = conversation_spans[-1]

    replay_generate = _GenerateRecorder([_make_response(content="Replay concise: sunny.", prompt_tokens=18, completion_tokens=4)])
    monkeypatch.setattr("ai_pipeline_core.llm.conversation.core_generate", replay_generate)
    replayed = await execute_span(target_span.span_id, source_db=snapshot)

    assert replay_generate.requests == [original_generate.requests[-1]]
    assert replayed.content == "Replay concise: sunny."

    first_conversation_input = json.loads(conversation_spans[0].input_json)
    assert first_conversation_input["tools"]["items"][0]["name"] == "history_search_tool"
