#!/usr/bin/env python3
"""MemoryDatabase showcase focused on recorded span metadata.

Usage:
  python examples/showcase_database.py
"""

import asyncio
import json
from typing import override

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FlowOptions, FlowOutputs, FlowStep, PipelineDeployment, PipelineFlow, PipelineTask
from ai_pipeline_core.database import DatabaseReader, SpanKind, SpanRecord
from ai_pipeline_core.database._memory import _MemoryDatabase

SUMMARY_PREVIEW_CHARS = 60
RESULT_PREVIEW_CHARS = 200


class RawDataDocument(Document):
    """Root input stored at the deployment boundary."""


class CleanedDataDocument(Document):
    """Normalized output from cleaning."""


class SummaryReportDocument(Document):
    """Final summary produced from cleaned documents."""


class CleanDataTask(PipelineTask):
    """Normalize root inputs before summarization."""

    name = "clean-data"

    @classmethod
    async def run(cls, raw_documents: tuple[RawDataDocument, ...]) -> tuple[CleanedDataDocument, ...]:
        return tuple(
            CleanedDataDocument.derive(
                derived_from=(raw,),
                name=f"cleaned_{raw.name}",
                content=" ".join(raw.text.split()).upper(),
            )
            for raw in raw_documents
        )


class BuildSummaryTask(PipelineTask):
    """Compile cleaned outputs into a single markdown summary."""

    name = "build-summary"

    @classmethod
    async def run(cls, cleaned_documents: tuple[CleanedDataDocument, ...]) -> tuple[SummaryReportDocument, ...]:
        lines = ["# Summary", "", f"Total documents: {len(cleaned_documents)}", ""]
        for index, document in enumerate(cleaned_documents, start=1):
            lines.append(f"- Doc {index} ({document.name}): {document.text[:SUMMARY_PREVIEW_CHARS]}")
        return (
            SummaryReportDocument.derive(
                derived_from=cleaned_documents,
                name="summary.md",
                content="\n".join(lines),
            ),
        )


class CleaningFlow(PipelineFlow):
    """First flow: normalize raw inputs."""

    @override
    async def run(
        self,
        raw_documents: tuple[RawDataDocument, ...],
        options: FlowOptions,
    ) -> tuple[CleanedDataDocument, ...]:
        _ = options
        return await CleanDataTask.run(raw_documents=raw_documents)


class SummaryFlow(PipelineFlow):
    """Second flow: build a final summary."""

    @override
    async def run(
        self,
        cleaned_documents: tuple[CleanedDataDocument, ...],
        options: FlowOptions,
    ) -> tuple[SummaryReportDocument, ...]:
        _ = options
        return await BuildSummaryTask.run(cleaned_documents=cleaned_documents)


class DatabaseShowcaseResult(DeploymentResult):
    """Small result model for the example deployment."""

    summary_preview: str = ""
    document_count: int = 0


class DatabaseShowcasePipeline(PipelineDeployment[FlowOptions, DatabaseShowcaseResult]):
    """Minimal deployment used to populate _MemoryDatabase."""

    @override
    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(CleaningFlow()), FlowStep(SummaryFlow())))

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: FlowOptions,
    ) -> DatabaseShowcaseResult:
        _ = (run_id, options)
        outputs = FlowOutputs(documents)
        summary = outputs.latest(SummaryReportDocument)
        if summary is None:
            return DatabaseShowcaseResult(success=False, error="No summary produced")
        return DatabaseShowcaseResult(
            success=True,
            summary_preview=summary.text[:RESULT_PREVIEW_CHARS],
            document_count=len(documents),
        )


def _select_span(tree: list[SpanRecord], kind: SpanKind, name: str) -> SpanRecord:
    for span in tree:
        if span.kind == kind and span.name == name:
            return span
    raise RuntimeError(f"Could not find span {kind}:{name} in the deployment tree.")


async def main() -> None:
    database = _MemoryDatabase()
    reader: DatabaseReader = database
    pipeline = DatabaseShowcasePipeline()

    result = await pipeline.run(
        "examples-database-main",
        (
            RawDataDocument.create_root(
                name="main_file_a.txt",
                content="First raw document with duplicate onboarding steps.",
                reason="seed the main database showcase run",
            ),
            RawDataDocument.create_root(
                name="main_file_b.txt",
                content="Second raw document with inconsistent button labels.",
                reason="seed the main database showcase run",
            ),
        ),
        FlowOptions(),
        database=database,
    )

    deployment = await reader.get_deployment_by_run_id("examples-database-main")
    if deployment is None:
        raise RuntimeError("Database showcase deployment was not recorded.")

    tree = await reader.get_deployment_tree(deployment.root_deployment_id)
    summary_task_span = _select_span(tree, SpanKind.TASK, BuildSummaryTask.name)
    task_meta = json.loads(summary_task_span.meta_json or "{}")
    task_metrics = json.loads(summary_task_span.metrics_json or "{}")

    document_shas = await reader.get_all_document_shas_for_tree(deployment.root_deployment_id)
    documents_by_sha = await reader.get_documents_batch(sorted(document_shas))
    documents = sorted(documents_by_sha.values(), key=lambda record: record.name)

    print("Deployment result:")
    print(f"  - success: {result.success}")
    print(f"  - document_count: {result.document_count}")
    print(f"  - summary_preview: {result.summary_preview}")

    print("\nRecorded span:")
    print(f"  - kind: {summary_task_span.kind}")
    print(f"  - name: {summary_task_span.name}")
    print(f"  - target: {summary_task_span.target}")
    print(f"  - meta_json: {json.dumps(task_meta, indent=2, sort_keys=True)}")
    print(f"  - metrics_json: {json.dumps(task_metrics, indent=2, sort_keys=True)}")

    print("\nOutput documents:")
    for document in documents:
        print(f"  - {document.name} [{document.document_type}]")

    await database.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
