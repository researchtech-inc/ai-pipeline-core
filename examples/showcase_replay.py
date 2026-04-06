#!/usr/bin/env python3
"""Replay showcase using a real recorded task span.

Usage:
  python examples/showcase_replay.py
"""

import asyncio

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FlowOptions, FlowStep, PipelineDeployment, PipelineFlow, PipelineTask
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.replay import execute_span


class ReplaySourceDocument(Document):
    """Input document for the replay showcase."""


class ReplayOutputDocument(Document):
    """Output document returned by the replayed task."""


class ReplayUppercaseTask(PipelineTask):
    """Uppercase one source document."""

    name = "replay_uppercase"

    @classmethod
    async def run(cls, sources: tuple[ReplaySourceDocument, ...]) -> tuple[ReplayOutputDocument, ...]:
        source = sources[0]
        return (
            ReplayOutputDocument.derive(
                derived_from=(source,),
                name="replayed_notes.txt",
                content=source.text.upper(),
            ),
        )


class ReplayFlow(PipelineFlow):
    """Single-task flow used to record a replayable task span."""

    async def run(
        self,
        sources: tuple[ReplaySourceDocument, ...],
        options: FlowOptions,
    ) -> tuple[ReplayOutputDocument, ...]:
        _ = options
        return await ReplayUppercaseTask.run(sources=sources)


class ReplayShowcaseResult(DeploymentResult):
    """Deployment result for the replay example."""

    output_count: int = 0


class ReplayShowcasePipeline(PipelineDeployment[FlowOptions, ReplayShowcaseResult]):
    """Minimal deployment that records one replayable task span."""

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(ReplayFlow()),))

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: FlowOptions,
    ) -> ReplayShowcaseResult:
        _ = (run_id, options)
        return ReplayShowcaseResult(success=True, output_count=len(documents))


def _select_recorded_task_span(tree: tuple[object, ...]) -> object:
    for span in tree:
        if getattr(span, "kind", "") != SpanKind.TASK:
            continue
        target = getattr(span, "target", "")
        if target.endswith(f"{ReplayUppercaseTask.__qualname__}.run"):
            return span
    raise RuntimeError("Could not find the recorded ReplayUppercaseTask span in the deployment tree.")


async def main() -> None:
    source_database = _MemoryDatabase()
    replay_database = _MemoryDatabase()
    pipeline = ReplayShowcasePipeline()

    input_document = ReplaySourceDocument.create_root(
        name="notes.txt",
        content="Replay should execute against a span recorded by the real pipeline runtime.",
        reason="seed the replay showcase input",
    )

    await pipeline.run(
        "examples-replay",
        (input_document,),
        FlowOptions(),
        database=source_database,
    )

    deployment = await source_database.get_deployment_by_run_id("examples-replay")
    if deployment is None:
        raise RuntimeError("Replay showcase deployment was not recorded in the source database.")

    tree = tuple(await source_database.get_deployment_tree(deployment.root_deployment_id))
    recorded_task_span = _select_recorded_task_span(tree)
    replay_outputs = await execute_span(
        recorded_task_span.span_id,
        source_db=source_database,
        sink_db=replay_database,
    )

    replay_deployments = await replay_database.list_deployments(limit=5)

    print("Recorded span:")
    print(f"  - span_id: {recorded_task_span.span_id}")
    print(f"  - target: {recorded_task_span.target}")
    print(f"  - input_document_shas: {recorded_task_span.input_document_shas}")

    print("\nReplay result:")
    for document in replay_outputs:
        print(f"  - {document.name}: {document.text}")

    print("\nReplay recording:")
    print(f"  - replay deployments captured: {len(replay_deployments)}")
    if replay_deployments:
        print(f"  - replay run_id: {replay_deployments[0].run_id}")

    await source_database.shutdown()
    await replay_database.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
