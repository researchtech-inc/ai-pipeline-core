#!/usr/bin/env python3
"""Stub classes showcase — partial pipeline implementation.

Demonstrates how to use ``_stub = True`` to define placeholder classes with
correct type signatures that pass all validation but block deployment.

This pattern enables multi-agent development workflows where:
- An architect agent defines the pipeline structure with stubs
- Flow agents implement flows and create task stubs
- Task agents implement individual tasks
- PromptSpec agents refine prompt specifications

Usage::

    python examples/showcase_stubs.py

This example imports successfully, validates all type signatures at import time,
and demonstrates that implemented flows run while stub flows raise StubNotImplementedError.
"""

import asyncio
import logging

from ai_pipeline_core import (
    Document,
    FlowOptions,
    PipelineFlow,
    PipelineTask,
    PromptSpec,
    Role,
    StubNotImplementedError,
    pipeline_test_context,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Document types (defined by architect)
# ---------------------------------------------------------------------------


class RawDataDoc(Document):
    """Raw input data for analysis."""


class CleanedDataDoc(Document):
    """Cleaned and normalized data."""


class AnalysisResultDoc(Document):
    """Analysis findings from processed data."""


class FinalReportDoc(Document):
    """Final synthesized report."""


# ---------------------------------------------------------------------------
# Implemented task — ready to run
# ---------------------------------------------------------------------------


class CleanDataTask(PipelineTask):
    """Clean and normalize raw input data.

    Input: RawDataDoc with unprocessed content.
    Output: CleanedDataDoc with normalized content.
    """

    @classmethod
    async def run(cls, raw_documents: tuple[RawDataDoc, ...]) -> tuple[CleanedDataDoc, ...]:
        results: list[CleanedDataDoc] = []
        for doc in raw_documents:
            cleaned = CleanedDataDoc.derive(
                derived_from=(doc,),
                name=f"cleaned_{doc.name}",
                content=doc.text.strip().lower(),
            )
            results.append(cleaned)
        return tuple(results)


# ---------------------------------------------------------------------------
# Stub task — awaiting implementation by task agent
# ---------------------------------------------------------------------------


class AnalyzeDataTask(PipelineTask, stub=True):
    """Analyze cleaned data and produce structured findings.

    Input: CleanedDataDoc with normalized content.
    Output: AnalysisResultDoc with analysis findings including:
    - Key themes identified
    - Statistical summaries
    - Anomaly detection results

    Implementation notes:
    - Use gemini-3-flash for initial analysis
    - Use structured output with AnalysisFindings model
    - Split analysis into theme extraction and statistical summary
    """

    @classmethod
    async def run(cls, cleaned_documents: tuple[CleanedDataDoc, ...]) -> tuple[AnalysisResultDoc, ...]: ...


class SynthesizeReportTask(PipelineTask, stub=True):
    """Synthesize analysis results into a final markdown report.

    Input: AnalysisResultDoc with analysis findings.
    Output: FinalReportDoc with formatted markdown report.

    Implementation notes:
    - Use conversational follow-up pattern for long report generation
    - Include executive summary, detailed findings, and recommendations
    """

    @classmethod
    async def run(cls, analysis_results: tuple[AnalysisResultDoc, ...]) -> tuple[FinalReportDoc, ...]: ...


# ---------------------------------------------------------------------------
# Implemented flow — runs CleanDataTask (implemented)
# ---------------------------------------------------------------------------


class DataCleaningFlow(PipelineFlow):
    """Clean and normalize raw input documents.

    Input: RawDataDoc from external sources.
    Output: CleanedDataDoc ready for analysis.
    """

    async def run(self, raw_documents: tuple[RawDataDoc, ...], options: FlowOptions) -> tuple[CleanedDataDoc, ...]:
        _ = options
        return await CleanDataTask.run(raw_documents=raw_documents)


# ---------------------------------------------------------------------------
# Stub flow — awaiting implementation
# ---------------------------------------------------------------------------


class AnalysisFlow(PipelineFlow, stub=True):
    """Analyze cleaned data and produce findings.

    Input: CleanedDataDoc from cleaning flow.
    Output: AnalysisResultDoc with structured findings.
    """

    async def run(self, cleaned_documents: tuple[CleanedDataDoc, ...], options: FlowOptions) -> tuple[AnalysisResultDoc, ...]:
        _ = (cleaned_documents, options)


class ReportFlow(PipelineFlow, stub=True):
    """Synthesize analysis into final report.

    Input: AnalysisResultDoc from analysis flow.
    Output: FinalReportDoc with formatted report.
    """

    async def run(self, analysis_results: tuple[AnalysisResultDoc, ...], options: FlowOptions) -> tuple[FinalReportDoc, ...]:
        _ = (analysis_results, options)


# ---------------------------------------------------------------------------
# Stub PromptSpec — architectural placeholder
# ---------------------------------------------------------------------------


class AnalystRole(Role):
    """Senior data analyst with domain expertise."""

    text = "You are a senior data analyst specializing in pattern recognition and statistical analysis"


class AnalysisSpec(PromptSpec, stub=True):
    """Analyze cleaned data for key themes and anomalies."""

    role = AnalystRole
    input_documents = (CleanedDataDoc,)
    task = "Analyze the provided cleaned data documents for key themes, statistical patterns, and anomalies."


# ---------------------------------------------------------------------------
# Demo: implemented flow runs, stub flow raises StubNotImplementedError
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # 1. Import succeeds — all stubs pass validation
    logger.info("All classes imported and validated successfully")
    logger.info("  Implemented: CleanDataTask, DataCleaningFlow")
    logger.info("  Stubs: AnalyzeDataTask, SynthesizeReportTask, AnalysisFlow, ReportFlow, AnalysisSpec")

    # 2. Type contracts are preserved on stubs
    logger.info("AnalyzeDataTask input types: %s", [t.__name__ for t in AnalyzeDataTask.input_document_types])
    logger.info("AnalyzeDataTask output types: %s", [t.__name__ for t in AnalyzeDataTask.output_document_types])
    logger.info("AnalysisFlow input types: %s", [t.__name__ for t in AnalysisFlow.input_document_types])
    logger.info("AnalysisFlow output types: %s", [t.__name__ for t in AnalysisFlow.output_document_types])

    # 3. Implemented flow runs successfully
    async def run_demo() -> None:
        with pipeline_test_context():
            raw = RawDataDoc.create_root(reason="demo input", name="sample.txt", content="  Hello World  ")
            result = await DataCleaningFlow().run(raw_documents=(raw,), options=FlowOptions())
            logger.info("DataCleaningFlow produced: %s (content: %r)", result[0].name, result[0].text)

    asyncio.run(run_demo())

    # 4. Stub task raises StubNotImplementedError
    async def run_stub_demo() -> None:
        with pipeline_test_context():
            doc = CleanedDataDoc.create_root(reason="demo", name="test.txt", content="test")
            try:
                await AnalyzeDataTask.run(cleaned_documents=(doc,))
            except StubNotImplementedError as e:
                logger.info("Stub task correctly raised: %s", e)

    asyncio.run(run_stub_demo())

    # 5. Stub registry (only non-exempt app classes are registered for deploy blocking)
    from ai_pipeline_core.pipeline._file_rules import get_stubs

    stubs = get_stubs()
    logger.info("Registered stubs for deploy blocking: %d (examples are exempt)", len(stubs))

    # 6. Verify _stub attribute on classes directly
    for cls in (AnalyzeDataTask, SynthesizeReportTask, AnalysisFlow, ReportFlow, AnalysisSpec):
        logger.info("  %s._stub = %s", cls.__name__, cls._stub)


if __name__ == "__main__":
    main()
