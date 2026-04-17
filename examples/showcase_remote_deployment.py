#!/usr/bin/env python3
"""Remote deployment showcase: Competitive Intelligence Pipeline.

Demonstrates how a parent deployment delegates work to child deployments
via RemoteDeployment, with the full span tree linked in the database.

Architecture::

    Parent: CompetitiveIntelPipeline
        ├── TriageFlow            → classify competitor briefs (structured output)
        ├── DeepAnalysisFlow      → delegate each competitor to child via RemoteDeployment
        │   └── CompetitorAnalyzer (RemoteDeployment, inline mode)
        │       └── CompetitorAnalysisPipeline (child deployment)
        │           ├── ResearchFlow      → tool-assisted research
        │           ├── AssessmentFlow    → dual-perspective analysis
        │           └── ScoringFlow       → structured threat scoring
        └── SynthesisFlow         → compile landscape report with traced_operation

Features demonstrated:
    - RemoteDeployment with inline fallback
    - Parent-to-child span linkage (visible via ai-trace)
    - Parallel LLM calls sharing cached context
    - Tool use within child deployment
    - Structured output via send_structured
    - traced_operation for lightweight operation spans
    - safe_gather for parallel remote deployment calls
    - Multi-level document provenance chains

Usage::

    python examples/showcase_remote_deployment.py ./output

Inspect the execution tree::

    ai-trace show <deployment-id> --db-path ./output
    ai-trace download <deployment-id> -o ./download --db-path ./output
    ai-replay show --from-db <span-id> --db-path ./output
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
    RemoteDeployment,
    Tool,
    get_run_id,
    safe_gather,
    traced_operation,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Shared configuration
# =============================================================================


class PipelineConfig(BaseModel, frozen=True):
    core_model: str
    fast_model: str
    reasoning_effort: Literal["low", "medium", "high"]


class PipelineConfigDocument(Document[PipelineConfig]):
    """Model configuration shared across parent and child deployments."""


class CompetitiveIntelOptions(FlowOptions):
    core_model: str = "gemini-3-flash"
    fast_model: str = "gemini-3-flash"
    reasoning_effort: Literal["low", "medium", "high"] = "low"


# =============================================================================
# Document types — Parent pipeline
# =============================================================================


class CompetitorBriefDocument(Document):
    """Raw competitor brief provided as pipeline input."""


class TriageModel(BaseModel, frozen=True):
    competitor_name: str
    category: Literal["direct", "indirect", "emerging"]
    priority: Literal["high", "medium", "low"]
    key_products: list[str] = Field(default_factory=list)
    rationale: str = ""


class TriagedCompetitorDocument(Document[TriageModel]):
    """Classified competitor brief with priority and category."""


class CompetitorAnalysisDocument(Document):
    """Analysis results received from child deployment, wrapped as document."""


class LandscapeReportDocument(Document):
    """Final competitive landscape synthesis report."""


# =============================================================================
# Document types — Child pipeline
# =============================================================================


class CompetitorInputDocument(Document):
    """Competitor brief passed to the child deployment for deep analysis."""


class CompetitorResearchDocument(Document):
    """Enriched research from tool-assisted investigation."""


class StrengthsDocument(Document):
    """Optimistic perspective on competitor position."""


class WeaknessesDocument(Document):
    """Critical perspective on competitor vulnerabilities."""


class ThreatScore(BaseModel, frozen=True):
    market_overlap: Literal["none", "partial", "significant", "direct"]
    technology_risk: Literal["low", "medium", "high", "critical"]
    growth_trajectory: Literal["declining", "stable", "growing", "accelerating"]
    overall_threat: Literal["low", "medium", "high", "critical"]
    recommended_response: str
    confidence_factors: list[str] = Field(default_factory=list)


class ThreatScoreDocument(Document[ThreatScore]):
    """Structured threat assessment for a competitor."""


# =============================================================================
# Tools — Market intelligence lookup
# =============================================================================

MARKET_DATA = {
    "acme": {"revenue": "$50M", "growth": "25% YoY", "market_share": "15%", "funding": "Series C, $80M"},
    "globex": {"revenue": "$120M", "growth": "10% YoY", "market_share": "30%", "funding": "Public (GLBX)"},
    "initech": {"revenue": "$30M", "growth": "40% YoY", "market_share": "8%", "funding": "Series B, $45M"},
    "umbrella": {"revenue": "$200M", "growth": "5% YoY", "market_share": "35%", "funding": "Public (UMBR)"},
    "cyberdyne": {"revenue": "$15M", "growth": "80% YoY", "market_share": "3%", "funding": "Series A, $20M"},
}

INDUSTRY_TRENDS = {
    "ai platform": (
        "The AI platform market is projected to reach $150B by 2028. Key differentiators: model quality, developer experience, enterprise compliance."
    ),
    "data pipeline": ("Data pipeline tools are commoditizing. Winners differentiate on real-time processing, observability, and integration breadth."),
    "developer tools": ("Developer tool adoption is bottom-up. Key metrics: time-to-value, documentation quality, and community size."),
    "enterprise software": "Enterprise buyers prioritize security, compliance, and integration with existing stacks over innovation speed.",
}


class LookupMarketData(Tool):
    """Look up market data for a company including revenue, growth, market share, and funding status."""

    class Input(BaseModel):
        company_name: str = Field(description="Company name to look up (case-insensitive)")

    class Output(BaseModel):
        company: str
        data: dict[str, str] | None
        available_companies: list[str] | None = None

    async def run(self, input: Input) -> Output:
        key = input.company_name.lower().strip()
        for name, data in MARKET_DATA.items():
            if name in key or key in name:
                return self.Output(company=input.company_name, data=data)
        return self.Output(
            company=input.company_name,
            data=None,
            available_companies=list(MARKET_DATA.keys()),
        )


class LookupIndustryTrends(Tool):
    """Look up industry trends and competitive dynamics for a market segment."""

    class Input(BaseModel):
        segment: str = Field(description="Market segment or industry to research")

    class Output(BaseModel):
        segment: str
        trends: str | None
        available_segments: list[str] | None = None

    async def run(self, input: Input) -> Output:
        key = input.segment.lower().strip()
        for segment, intel in INDUSTRY_TRENDS.items():
            if any(word in key for word in segment.split()):
                return self.Output(segment=segment, trends=intel)
        return self.Output(
            segment=input.segment,
            trends=None,
            available_segments=list(INDUSTRY_TRENDS.keys()),
        )


# =============================================================================
# Child pipeline: CompetitorAnalysisPipeline
# =============================================================================


class CompetitorAnalysisResult(DeploymentResult):
    """Result returned by the child deployment to the parent."""

    competitor_name: str = ""
    threat_level: str = ""
    strengths_summary: str = ""
    weaknesses_summary: str = ""
    recommended_response: str = ""


# --- Research flow (tool use) ---


class ResearchTask(PipelineTask):
    name = "competitor_research"

    @classmethod
    async def run(cls, brief: CompetitorInputDocument, config: PipelineConfigDocument) -> tuple[CompetitorResearchDocument, ...]:
        cfg = config.parsed
        tools = [LookupMarketData(), LookupIndustryTrends()]
        conv = Conversation(model=cfg.core_model).with_context(brief)
        conv = await conv.send(
            "Research this competitor thoroughly:\n"
            "1. Look up their market data (revenue, growth rate, market share, funding)\n"
            "2. Look up industry trends for their primary market segment\n"
            "Synthesize all findings into a concise research brief.",
            tools=tools,
            purpose=f"research {brief.name}",
        )
        return (
            CompetitorResearchDocument.derive(
                derived_from=(brief,),
                name=f"research_{brief.id}.md",
                content=conv.content,
            ),
        )


class ResearchFlow(PipelineFlow):
    estimated_minutes = 2

    async def run(
        self,
        brief: CompetitorInputDocument,
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[CompetitorResearchDocument, ...]:
        _ = options
        return await ResearchTask.run(brief=brief, config=config)


# --- Assessment flow (dual-perspective analysis) ---


class AssessmentTask(PipelineTask):
    name = "dual_assessment"

    @classmethod
    async def run(
        cls,
        brief: CompetitorInputDocument,
        research: CompetitorResearchDocument,
        config: PipelineConfigDocument,
    ) -> tuple[StrengthsDocument, WeaknessesDocument]:
        cfg = config.parsed

        # Warmup: populate provider cache with shared context (brief + research)
        conv = Conversation(model=cfg.core_model).with_context(brief, research)
        warmup = await conv.send(
            "I will ask you to analyze this competitor from two different perspectives. "
            "Please confirm you have read and understood the competitor brief and research data.",
            purpose="warmup shared context",
        )

        # Fork: parallel optimistic and pessimistic analysis off the same cached prefix
        strengths_conv, weaknesses_conv = await safe_gather(
            warmup.send(
                "Present the STRENGTHS perspective on this competitor. "
                "Argue why they are well-positioned: strong products, market advantages, "
                "growth trajectory, funding, team capabilities. "
                "Be specific, cite evidence from the research data.",
                purpose="strengths analysis",
            ),
            warmup.send(
                "Present the WEAKNESSES perspective on this competitor. "
                "Argue where they are vulnerable: market gaps, technical debt, "
                "scaling challenges, competitive pressure, funding risks. "
                "Be specific, cite evidence from the research data.",
                purpose="weaknesses analysis",
            ),
            label="dual-perspective-fork",
        )

        return (
            StrengthsDocument.derive(
                derived_from=(brief, research),
                name=f"strengths_{brief.id}.md",
                content=strengths_conv.content,
            ),
            WeaknessesDocument.derive(
                derived_from=(brief, research),
                name=f"weaknesses_{brief.id}.md",
                content=weaknesses_conv.content,
            ),
        )


class AssessmentFlow(PipelineFlow):
    estimated_minutes = 3

    async def run(
        self,
        brief: CompetitorInputDocument,
        research: CompetitorResearchDocument,
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[StrengthsDocument | WeaknessesDocument, ...]:
        _ = options
        return await AssessmentTask.run(brief=brief, research=research, config=config)


# --- Scoring flow (structured output) ---


class ScoringTask(PipelineTask):
    name = "threat_scoring"

    @classmethod
    async def run(
        cls,
        strengths: StrengthsDocument,
        weaknesses: WeaknessesDocument,
        brief: CompetitorInputDocument,
        config: PipelineConfigDocument,
    ) -> tuple[ThreatScoreDocument, ...]:
        cfg = config.parsed

        options = ModelOptions(reasoning_effort=cfg.reasoning_effort)
        conv = Conversation(model=cfg.fast_model, model_options=options).with_context(brief, strengths, weaknesses)
        conv = await conv.send_structured(
            "Based on the competitor brief, strengths analysis, and weaknesses analysis, "
            "produce a structured threat assessment. Evaluate each dimension carefully.",
            response_format=ThreatScore,
            purpose=f"threat score {brief.name}",
        )
        parsed = conv.parsed
        if parsed is None:
            raise RuntimeError(f"Structured output parsing failed for {brief.name}")

        return (
            ThreatScoreDocument.derive(
                derived_from=(brief, strengths, weaknesses),
                name=f"threat_score_{brief.id}.json",
                content=parsed,
            ),
        )


class ScoringFlow(PipelineFlow):
    estimated_minutes = 1

    async def run(
        self,
        strengths: StrengthsDocument,
        weaknesses: WeaknessesDocument,
        brief: CompetitorInputDocument,
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[ThreatScoreDocument, ...]:
        _ = options
        return await ScoringTask.run(strengths=strengths, weaknesses=weaknesses, brief=brief, config=config)


# --- Child deployment assembly ---

RESULT_SUMMARY_MAX_LENGTH = 500


class CompetitorAnalysisPipeline(PipelineDeployment[CompetitiveIntelOptions, CompetitorAnalysisResult]):
    """Child deployment: deep analysis of a single competitor.

    Called by the parent via RemoteDeployment. In production this runs on a
    separate Prefect worker; in this example it falls back to inline execution.
    """

    def build_plan(self, options: CompetitiveIntelOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(ResearchFlow()), FlowStep(AssessmentFlow()), FlowStep(ScoringFlow())))

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: CompetitiveIntelOptions,
    ) -> CompetitorAnalysisResult:
        _ = (run_id, options)
        outputs = FlowOutputs(documents)
        briefs = outputs.all(CompetitorInputDocument)
        scores = outputs.all(ThreatScoreDocument)
        strengths = outputs.all(StrengthsDocument)
        weaknesses = outputs.all(WeaknessesDocument)

        score = scores[0].parsed if scores else None
        return CompetitorAnalysisResult(
            success=True,
            competitor_name=briefs[0].name.removesuffix(".md") if briefs else "unknown",
            threat_level=score.overall_threat if score else "unknown",
            strengths_summary=strengths[0].text[:RESULT_SUMMARY_MAX_LENGTH] if strengths else "",
            weaknesses_summary=weaknesses[0].text[:RESULT_SUMMARY_MAX_LENGTH] if weaknesses else "",
            recommended_response=score.recommended_response if score else "",
        )


# =============================================================================
# RemoteDeployment client — parent calls child through this typed interface
# =============================================================================


class CompetitorAnalyzer(RemoteDeployment[CompetitiveIntelOptions, CompetitorAnalysisResult]):
    """Typed client for the competitor analysis child deployment.

    In production: runs via Prefect on a remote worker (ClickHouseDatabase).
    In this example: falls back to inline mode (FilesystemDatabase/_MemoryDatabase).
    """

    deployment_class = f"{__name__}:CompetitorAnalysisPipeline"


# =============================================================================
# Parent pipeline: CompetitiveIntelPipeline
# =============================================================================


# --- Triage flow (structured output classification) ---


class TriageTask(PipelineTask):
    name = "triage_competitors"

    @classmethod
    async def run(
        cls,
        briefs: tuple[CompetitorBriefDocument, ...],
        config: PipelineConfigDocument,
    ) -> tuple[TriagedCompetitorDocument, ...]:
        cfg = config.parsed

        results: list[TriagedCompetitorDocument] = []
        for brief in briefs:
            conv = Conversation(model=cfg.fast_model).with_context(brief)
            conv = await conv.send_structured(
                "Classify this competitor brief. Determine the competitor name, "
                "whether they are a direct/indirect/emerging competitor to us "
                "(an AI pipeline framework company), their priority level, "
                "and list their key products.",
                response_format=TriageModel,
                purpose=f"triage {brief.name}",
            )
            parsed = conv.parsed
            if parsed is None:
                raise RuntimeError(f"Triage structured output failed for {brief.name}")
            results.append(
                TriagedCompetitorDocument.derive(
                    derived_from=(brief,),
                    name=f"triage_{brief.id}.json",
                    content=parsed,
                )
            )
        return tuple(results)


class TriageFlow(PipelineFlow):
    estimated_minutes = 2

    async def run(
        self,
        briefs: tuple[CompetitorBriefDocument, ...],
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[TriagedCompetitorDocument, ...]:
        _ = options
        logger.info("Triaging %d competitor briefs", len(briefs))
        return await TriageTask.run(briefs=briefs, config=config)


# --- Deep analysis flow (delegates to child deployments via RemoteDeployment) ---


class AnalyzeCompetitorTask(PipelineTask):
    """Delegate a single competitor to the child deployment and wrap its result."""

    name = "analyze_competitor"

    @classmethod
    async def run(
        cls,
        triage: TriagedCompetitorDocument,
        config: PipelineConfigDocument,
    ) -> tuple[CompetitorAnalysisDocument, ...]:
        parsed = triage.parsed

        # Prepare child deployment inputs
        child_input = CompetitorInputDocument.derive(
            derived_from=(triage,),
            name=f"{parsed.competitor_name.lower().replace(' ', '_')}.md",
            content=(
                f"Competitor: {parsed.competitor_name}\n"
                f"Category: {parsed.category}\n"
                f"Priority: {parsed.priority}\n"
                f"Key Products: {', '.join(parsed.key_products)}\n"
                f"Rationale: {parsed.rationale}"
            ),
        )
        child_config = PipelineConfigDocument.derive(
            derived_from=(config,),
            name="child_config.json",
            content=config.parsed,
        )

        # Call child deployment via RemoteDeployment (inline fallback)
        analyzer = CompetitorAnalyzer()
        options = CompetitiveIntelOptions(
            core_model=config.parsed.core_model,
            fast_model=config.parsed.fast_model,
            reasoning_effort=config.parsed.reasoning_effort,
        )
        result = await analyzer.run((child_input, child_config), options)

        return (
            CompetitorAnalysisDocument.derive(
                derived_from=(triage,),
                name=f"analysis_{parsed.competitor_name.lower().replace(' ', '_')}.md",
                content=(
                    f"# {result.competitor_name} — Threat Level: {result.threat_level.upper()}\n\n"
                    f"## Strengths\n{result.strengths_summary}\n\n"
                    f"## Weaknesses\n{result.weaknesses_summary}\n\n"
                    f"## Recommended Response\n{result.recommended_response}"
                ),
            ),
        )


class DeepAnalysisFlow(PipelineFlow):
    estimated_minutes = 10

    async def run(
        self,
        triaged_competitors: tuple[TriagedCompetitorDocument, ...],
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[CompetitorAnalysisDocument, ...]:
        _ = options
        logger.info("Dispatching %d competitor analyses via RemoteDeployment [%s]", len(triaged_competitors), get_run_id())

        # Run all competitor analyses in parallel via safe_gather
        analysis_docs = await safe_gather(
            *(AnalyzeCompetitorTask.run(triage=triage, config=config) for triage in triaged_competitors),
            label="parallel-competitor-analyses",
        )

        # Flatten: each task returns a tuple, safe_gather returns list of tuples
        return tuple(doc for result_tuple in analysis_docs for doc in result_tuple)


# --- Synthesis flow (with traced_operation) ---


class SynthesisTask(PipelineTask):
    name = "synthesize_landscape"

    @classmethod
    async def run(
        cls,
        analyses: tuple[CompetitorAnalysisDocument, ...],
        config: PipelineConfigDocument,
    ) -> tuple[LandscapeReportDocument, ...]:
        cfg = config.parsed

        async with traced_operation("compile_landscape", description="Compile all analyses into landscape report"):
            conv = Conversation(model=cfg.core_model).with_context(*analyses)
            conv = await conv.send(
                "You have received individual competitor analysis documents. "
                "Synthesize them into a comprehensive Competitive Landscape Report:\n\n"
                "1. **Executive Summary** — our overall competitive position\n"
                "2. **Threat Matrix** — rank competitors by threat level with justification\n"
                "3. **Cross-Cutting Trends** — patterns and themes across all competitors\n"
                "4. **Strategic Recommendations** — prioritized actions we should take\n\n"
                "Be concise and actionable. Reference specific competitors by name.",
                purpose="synthesize competitive landscape",
            )

        return (
            LandscapeReportDocument.derive(
                derived_from=tuple(analyses),
                name="competitive_landscape.md",
                content=conv.content,
            ),
        )


class SynthesisFlow(PipelineFlow):
    estimated_minutes = 3

    async def run(
        self,
        analyses: tuple[CompetitorAnalysisDocument, ...],
        config: PipelineConfigDocument,
        options: CompetitiveIntelOptions,
    ) -> tuple[LandscapeReportDocument, ...]:
        _ = options
        logger.info("Synthesizing landscape report from %d analyses", len(analyses))
        return await SynthesisTask.run(analyses=analyses, config=config)


# =============================================================================
# Parent deployment assembly
# =============================================================================


class CompetitiveIntelResult(DeploymentResult):
    report_document: LandscapeReportDocument
    competitors_analyzed: int = 0
    high_threats: int = 0


class CompetitiveIntelPipeline(PipelineDeployment[CompetitiveIntelOptions, CompetitiveIntelResult]):
    """Parent deployment: competitive intelligence analysis.

    Triages competitor briefs, delegates deep analysis of each competitor
    to a child deployment via RemoteDeployment, then synthesizes results
    into a competitive landscape report.
    """

    pubsub_service_type: ClassVar[str] = "competitive-intel"

    def build_plan(self, options: CompetitiveIntelOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(TriageFlow()), FlowStep(DeepAnalysisFlow()), FlowStep(SynthesisFlow())))

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: CompetitiveIntelOptions,
    ) -> CompetitiveIntelResult:
        _ = (run_id, options)
        outputs = FlowOutputs(documents)
        analyses = outputs.all(CompetitorAnalysisDocument)
        reports = outputs.all(LandscapeReportDocument)
        if not reports:
            raise RuntimeError("CompetitiveIntelPipeline expected a LandscapeReportDocument after the full plan completed.")
        high_count = sum(1 for a in analyses if "critical" in a.text.lower() or "high" in a.text.lower())
        return CompetitiveIntelResult(
            success=True,
            report_document=reports[0],
            competitors_analyzed=len(analyses),
            high_threats=high_count,
        )


# =============================================================================
# CLI entry point
# =============================================================================

competitive_intel_pipeline = CompetitiveIntelPipeline()


def initialize_competitive_intel(options: CompetitiveIntelOptions) -> tuple[str, tuple[Document, ...]]:
    """Create pipeline inputs: config + competitor briefs."""
    cfg = PipelineConfigDocument.create_root(
        name="pipeline_config.json",
        content=PipelineConfig(
            core_model=options.core_model,
            fast_model=options.fast_model,
            reasoning_effort=options.reasoning_effort,
        ),
        reason="pipeline model configuration",
    )
    docs: tuple[Document, ...] = (
        cfg,
        CompetitorBriefDocument.create_root(
            name="acme_corp.md",
            content=(
                "Acme Corp is a Series C startup building an AI-powered data pipeline platform. "
                "They recently launched a real-time streaming feature and claim 200+ enterprise customers. "
                "Their SDK supports Python, Go, and Rust. Key differentiator is built-in LLM observability. "
                "Founded in 2022, headquartered in San Francisco, 150 employees, growing 25% QoQ."
            ),
            reason="competitor brief for Acme Corp",
        ),
        CompetitorBriefDocument.create_root(
            name="cyberdyne_systems.md",
            content=(
                "Cyberdyne Systems is an early-stage startup (Series A, $20M raised) focused on autonomous "
                "AI agents for software development. Their product automates code review, testing, and "
                "deployment pipelines. Small team of 30 engineers but growing 80% YoY. Backed by prominent "
                "AI researchers from Stanford and DeepMind. Limited enterprise traction but strong developer "
                "community (15K GitHub stars). Their agent framework competes with our pipeline orchestration."
            ),
            reason="competitor brief for Cyberdyne Systems",
        ),
        CompetitorBriefDocument.create_root(
            name="globex_industries.md",
            content=(
                "Globex Industries (NASDAQ: GLBX) is a public company and market leader in enterprise data "
                "integration with $120M revenue and 30% market share. Growth has slowed to 10% YoY as they "
                "face disruption from AI-native startups. Strong brand, 2000+ enterprise customers, and "
                "established sales channels. Recent acquisitions of two AI startups ($50M combined) have not "
                "yet been integrated. Their legacy Java-based platform is being rewritten in Python/Go. "
                "Announced an 'AI Pipeline' product line competing directly with our core offering."
            ),
            reason="competitor brief for Globex Industries",
        ),
    )
    return "competitive-intel-q1-2026", docs


if __name__ == "__main__":
    competitive_intel_pipeline.run_cli(initializer=initialize_competitive_intel)
