#!/usr/bin/env python3
"""Class-based pipeline showcase.

Usage::

    python examples/showcase.py ./output

Execution data is persisted to the given working directory via FilesystemDatabase.
Inspect the stored execution with::

    ai-trace show <deployment-id> --db-path ./output
    ai-trace download <deployment-id> --db-path ./output
    ai-replay show --from-db <node-id> --db-path ./output
    ai-replay run --from-db <node-id> --db-path ./output
"""

import logging
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from ai_pipeline_core import (
    Conversation,
    DeploymentPlan,
    DeploymentResult,
    Document,
    FlowOptions,
    FlowOutputs,
    FlowStep,
    ModelOptions,
    PipelineDeployment,
    PipelineFlow,
    PipelineTask,
    Tool,
    get_run_id,
)

logger = logging.getLogger(__name__)


class InputDocument(Document):
    """Input text to analyze."""


class AnalysisDocument(Document):
    """Unstructured LLM analysis."""


class ReportDocument(Document):
    """Final markdown report."""


class ShowcaseConfig(BaseModel, frozen=True):
    core_model: str
    fast_model: str
    reasoning_effort: Literal["low", "medium", "high"]


class ShowcaseConfigDocument(Document[ShowcaseConfig]):
    """Root configuration for this run."""


class InsightModel(BaseModel, frozen=True):
    topics: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    complexity: Literal["low", "medium", "high"] = "low"


class InsightDocument(Document[InsightModel]):
    """Structured extraction output."""


class ResearchDocument(Document):
    """Research findings from tool-assisted investigation."""


# --- Tools for the research task ---

TOPIC_DATABASE = {
    "ai pipelines": "AI pipelines chain LLM calls with typed data flow, enabling reproducible multi-step reasoning.",
    "immutable documents": "Immutable documents ensure provenance tracking and safe parallel processing without race conditions.",
    "observability": "Pipeline observability captures execution trees, LLM metrics, and replay payloads for debugging.",
    "validation": "Import-time validation catches configuration errors before any LLM call is made, reducing wasted compute.",
}


class LookupRelatedTopics(Tool):
    """Look up topics related to a given subject from the knowledge base. Returns matching topic names and summaries."""

    class Input(BaseModel):
        subject: str = Field(description="The subject to find related topics for")

    class Output(BaseModel):
        topics: list[str]
        subject: str

    async def run(self, input: Input) -> Output:
        matches = [f"- **{topic}**: {summary}" for topic, summary in TOPIC_DATABASE.items() if any(word in topic for word in input.subject.lower().split())]
        return self.Output(topics=matches, subject=input.subject)


CLAIM_VERDICTS = {
    "provenance": "Confirmed: content-addressed SHA256 hashing provides full provenance chains.",
    "typed documents": "Confirmed: Pydantic-based Document subclasses enforce type safety at definition time.",
    "class-based tasks": "Confirmed: __init_subclass__ validates task signatures, return types, and config at import time.",
    "parallel processing": "Confirmed: immutability and frozen dataclasses enable safe concurrent execution.",
}


class VerifyClaim(Tool):
    """Check whether a factual claim about the system is accurate. Returns a verification verdict."""

    class Input(BaseModel):
        claim: str = Field(description="A short factual claim to verify")

    class Output(BaseModel):
        verdict: str
        verified: bool

    async def run(self, input: Input) -> Output:
        for keyword, verdict in CLAIM_VERDICTS.items():
            if keyword in input.claim.lower():
                return self.Output(verdict=verdict, verified=True)
        return self.Output(verdict=f"Unverified: no matching evidence found for '{input.claim}'.", verified=False)


class ExplainConcept(Tool):
    """Get a detailed explanation of a technical concept by consulting an expert LLM."""

    class Input(BaseModel):
        concept: str = Field(description="The technical concept to explain in depth")

    class Output(BaseModel):
        explanation: str

    def __init__(self, model: str) -> None:
        self._model = model

    async def run(self, input: Input) -> Output:
        conv = Conversation(model=self._model)
        conv = await conv.send(
            f"Explain '{input.concept}' in 2-3 sentences for a senior engineer. Be specific and technical.",
            purpose=f"explain {input.concept}",
        )
        return self.Output(explanation=conv.content)


class ShowcaseFlowOptions(FlowOptions):
    core_model: str = "gemini-3.1-pro"
    fast_model: str = "gemini-3-flash"
    reasoning_effort: Literal["low", "medium", "high"] = "medium"


class AnalyzeDocumentTask(PipelineTask):
    name = "analyze_document"

    @classmethod
    async def run(cls, source: InputDocument, config: ShowcaseConfigDocument) -> tuple[AnalysisDocument, ...]:
        logger.info("Running %s", cls.name)
        cfg = config.parsed
        conv = Conversation(model=cfg.core_model).with_context(source)
        conv = await conv.send(
            f"Analyze '{source.name}' and summarize key themes.",
            purpose=f"analyze {source.name}",
        )
        return (
            AnalysisDocument.derive(
                derived_from=(source,),
                name=f"analysis_{source.id}.md",
                content=conv.content,
            ),
        )


class ExtractInsightsTask(PipelineTask):
    name = "extract_insights"

    @classmethod
    async def run(cls, analysis: AnalysisDocument, config: ShowcaseConfigDocument) -> tuple[InsightDocument, ...]:
        logger.info("Running %s", cls.name)
        cfg = config.parsed
        options = ModelOptions(reasoning_effort=cfg.reasoning_effort)
        conv = Conversation(model=cfg.fast_model, model_options=options).with_context(analysis)
        conv = await conv.send_structured(
            "Extract structured insights.",
            response_format=InsightModel,
            purpose=f"extract insights from {analysis.name}",
        )
        parsed = conv.parsed
        if parsed is None:
            raise RuntimeError(f"Structured output parsing failed for '{analysis.name}'")
        return (
            InsightDocument.derive(
                derived_from=(analysis,),
                name=f"insight_{analysis.id}.json",
                content=parsed,
            ),
        )


class ResearchTask(PipelineTask):
    name = "research_with_tools"

    @classmethod
    async def run(cls, analysis: AnalysisDocument, config: ShowcaseConfigDocument) -> tuple[ResearchDocument, ...]:
        logger.info("Running %s", cls.name)
        cfg = config.parsed
        tools = [
            LookupRelatedTopics(),
            VerifyClaim(),
            ExplainConcept(model=cfg.fast_model),
        ]
        conv = Conversation(model=cfg.core_model).with_context(analysis)
        conv = await conv.send(
            "Based on the analysis document:\n"
            "1. Look up related topics for the main subject\n"
            "2. Verify one key factual claim made in the analysis\n"
            "3. Pick the most interesting concept and get a detailed explanation\n"
            "Then synthesize all findings into a concise research summary.",
            tools=tools,
            purpose=f"research {analysis.name}",
        )
        return (
            ResearchDocument.derive(
                derived_from=(analysis,),
                name=f"research_{analysis.id}.md",
                content=conv.content,
            ),
        )


class CompileReportTask(PipelineTask):
    name = "compile_report"

    @classmethod
    async def run(
        cls,
        insights: tuple[InsightDocument, ...],
        research_docs: tuple[ResearchDocument, ...],
    ) -> tuple[ReportDocument, ...]:
        logger.info("Running %s", cls.name)
        parsed_insights = [document.parsed for document in insights]
        lines = ["# Showcase Report", "", f"Insights: {len(parsed_insights)} | Research: {len(research_docs)}", ""]
        for idx, insight in enumerate(parsed_insights, start=1):
            lines.append(f"## Insight {idx}")
            lines.append(f"Complexity: {insight.complexity}")
            lines.extend(f"- {finding}" for finding in insight.findings)
            lines.append("")
        for idx, doc in enumerate(research_docs, start=1):
            lines.append(f"## Research {idx}")
            lines.append(doc.text)
            lines.append("")
        return (
            ReportDocument.derive(
                derived_from=(*insights, *research_docs),
                name="report.md",
                content="\n".join(lines),
            ),
        )


class AnalysisFlow(PipelineFlow):
    estimated_minutes = 5

    async def run(
        self,
        sources: tuple[InputDocument, ...],
        config: ShowcaseConfigDocument,
        options: ShowcaseFlowOptions,
    ) -> tuple[AnalysisDocument, ...]:
        logger.info("Running %s [%s]", type(self).name, get_run_id())
        _ = options
        outputs: list[AnalysisDocument] = []
        for source in sources:
            outputs.extend(await AnalyzeDocumentTask.run(source=source, config=config))
        return tuple(outputs)


class ExtractionFlow(PipelineFlow):
    estimated_minutes = 3

    async def run(
        self,
        analyses: tuple[AnalysisDocument, ...],
        config: ShowcaseConfigDocument,
        options: ShowcaseFlowOptions,
    ) -> tuple[InsightDocument, ...]:
        logger.info("Running %s [%s]", type(self).name, get_run_id())
        _ = options
        outputs: list[InsightDocument] = []
        for analysis in analyses:
            outputs.extend(await ExtractInsightsTask.run(analysis=analysis, config=config))
        return tuple(outputs)


class ResearchFlow(PipelineFlow):
    estimated_minutes = 3

    async def run(
        self,
        analyses: tuple[AnalysisDocument, ...],
        config: ShowcaseConfigDocument,
        options: ShowcaseFlowOptions,
    ) -> tuple[ResearchDocument, ...]:
        logger.info("Running %s [%s]", type(self).name, get_run_id())
        _ = options
        outputs: list[ResearchDocument] = []
        for analysis in analyses:
            outputs.extend(await ResearchTask.run(analysis=analysis, config=config))
        return tuple(outputs)


class ReportFlow(PipelineFlow):
    estimated_minutes = 1

    async def run(
        self,
        insights: tuple[InsightDocument, ...],
        research_docs: tuple[ResearchDocument, ...],
        options: ShowcaseFlowOptions,
    ) -> tuple[ReportDocument, ...]:
        logger.info("Running %s [%s]", type(self).name, get_run_id())
        _ = options
        return await CompileReportTask.run(insights=insights, research_docs=research_docs)


class ShowcaseResult(DeploymentResult):
    analysis_count: int = 0
    insight_count: int = 0
    research_count: int = 0
    report_count: int = 0


class ShowcasePipeline(PipelineDeployment[ShowcaseFlowOptions, ShowcaseResult]):
    pubsub_service_type: ClassVar[str] = "showcase"

    def build_plan(self, options: ShowcaseFlowOptions) -> DeploymentPlan:
        logger.info("Building flows for %s", type(self).__name__)
        _ = options
        return DeploymentPlan(steps=(FlowStep(AnalysisFlow()), FlowStep(ExtractionFlow()), FlowStep(ResearchFlow()), FlowStep(ReportFlow())))

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: ShowcaseFlowOptions,
    ) -> ShowcaseResult:
        _ = (run_id, options)
        outputs = FlowOutputs(documents)
        return ShowcaseResult(
            success=True,
            analysis_count=len(outputs.all(AnalysisDocument)),
            insight_count=len(outputs.all(InsightDocument)),
            research_count=len(outputs.all(ResearchDocument)),
            report_count=len(outputs.all(ReportDocument)),
        )


showcase_pipeline = ShowcasePipeline()


def initialize_showcase(options: ShowcaseFlowOptions) -> tuple[str, tuple[Document, ...]]:
    cfg = ShowcaseConfigDocument.create_root(
        name="showcase_config.json",
        content=ShowcaseConfig(
            core_model=options.core_model,
            fast_model=options.fast_model,
            reasoning_effort=options.reasoning_effort,
        ),
        reason="showcase configuration document",
    )
    docs: tuple[Document, ...] = (
        cfg,
        InputDocument.create_root(
            name="notes_a.txt",
            content="AI pipelines benefit from immutable typed documents and explicit provenance.",
            reason="showcase sample input A",
        ),
        InputDocument.create_root(
            name="notes_b.txt",
            content="Class-based tasks simplify import-time validation and observability.",
            reason="showcase sample input B",
        ),
    )
    return "showcase-v2", docs


if __name__ == "__main__":
    showcase_pipeline.run_cli(initializer=initialize_showcase)
