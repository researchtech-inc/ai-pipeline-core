#!/usr/bin/env python3
"""Showcase of ai_pipeline_core.prompt_compiler features.

Demonstrates every capability of the Prompt Compiler module:
  - Role: LLM persona definition
  - Rule: Behavioral constraints (max 5 lines)
  - OutputRule: Output formatting constraints (max 5 lines)
  - Guide: File-backed reference material (validated at import time)
  - PromptSpec: Typed prompt specification with import-time validation
  - follows: Typed follow-up spec chains
  - render_text(): Render spec instance to prompt string
  - render_preview(): Render spec class with placeholder values
  - Conversation.send_spec(): PromptSpec dispatch through the Conversation API
  - Shared tuples: Reuse config across specs without inheritance
  - Structured output: PromptSpec[BaseModel] dispatches to Conversation.send_structured()
  - Multi-turn: render_text() with include_input_documents=False for follow-ups
  - Import-time validation: Catches errors before runtime

No LLM connection required — all examples use render_text() and render_preview().

Usage:
  python examples/showcase_prompt_compiler.py
"""

from collections.abc import Callable
from types import new_class
from typing import Any

from pydantic import BaseModel, Field

from ai_pipeline_core.documents import Document
from ai_pipeline_core.prompt_compiler import (
    Guide,
    OutputRule,
    PromptSpec,
    Role,
    Rule,
    render_preview,
    render_text,
)

DISPLAY_PREVIEW_LINE_COUNT = 3

# =============================================================================
# 1. Document Types — what gets passed as context
# =============================================================================


class InitialWhitepaperDocument(Document):
    """The initial whitepaper provided by the project team."""


class PreliminaryResearchDocument(Document):
    """Preliminary research gathered from public sources."""


class IntermediateResearchDocument(Document):
    """Targeted research to investigate specific issues identified earlier."""


# =============================================================================
# 2. Roles — the identity the LLM assumes
# =============================================================================


class SeniorVCAnalyst(Role):
    """Debate participant in step 09 issue/opportunity analysis."""

    text = "Senior analyst at a top-tier venture capital firm"


class ResearchSupervisor(Role):
    """Final synthesis role that produces authoritative reports."""

    text = "Senior research supervisor responsible for final assessment quality"


# =============================================================================
# 3. Rules — behavioral constraints (max 5 lines each)
# =============================================================================


class BeAnalytical(Rule):
    """Ensures constructive tone in debate specs."""

    text = "Be analytical and constructive, not dismissive"


class FocusOnThisIssueOnly(Rule):
    """Prevents scope creep in single-issue analysis."""

    text = "Focus ONLY on the current issue — ignore other issues from research"


class CiteEvidence(Rule):
    """Requires grounding claims in document evidence."""

    text = """\
        Every claim must reference specific evidence from the provided documents.
        Use document IDs [ABC123] when citing."""


class NoSpeculation(Rule):
    """Prevents unfounded predictions."""

    text = "Do not speculate beyond what the evidence supports"


# =============================================================================
# 4. Output Rules — output formatting constraints
# =============================================================================


class StartWithOptimisticAnalyst(OutputRule):
    """Labels response for debate transcript parsing (optimistic side)."""

    text = 'Start response with "**Optimistic Analyst:**"'


class StartWithPessimisticAnalyst(OutputRule):
    """Labels response for debate transcript parsing (pessimistic side)."""

    text = 'Start response with "**Pessimistic Analyst:**"'


class DontUseMarkdownTables(OutputRule):
    """Prevents markdown tables which LLMs often format poorly."""

    text = "Do not use markdown tables — use bullet lists instead"


# =============================================================================
# 5. Guides — file-backed reference material (validated at import time)
# =============================================================================


class RiskAssessmentFramework(Guide):
    """Risk dimensions: Time Horizon, Likelihood, Impact, Complexity.
    Used by all issue debate specs."""

    template = "guides/risk_assessment_framework.txt"


class PositiveTeamAssumptions(Guide):
    """Baseline stakeholder assumptions for fair assessment.
    Used across both issue and opportunity analysis."""

    template = "guides/positive_team_assumptions.txt"


# =============================================================================
# 6. Shared Configuration — reuse without inheritance
# =============================================================================

STEP09_DOCUMENTS = (InitialWhitepaperDocument, PreliminaryResearchDocument, IntermediateResearchDocument)
STEP09_ISSUE_GUIDES = (RiskAssessmentFramework, PositiveTeamAssumptions)
STEP09_COMMON_RULES = (BeAnalytical, FocusOnThisIssueOnly, CiteEvidence)


# =============================================================================
# 7. PromptSpec — Full-featured text output spec
# =============================================================================


class IssueOptimisticSpec(PromptSpec):
    """Argue the optimistic case for a risk issue in the intermediate review debate."""

    input_documents = STEP09_DOCUMENTS
    role = SeniorVCAnalyst
    task = """\
        Present the OPTIMISTIC perspective on this issue.
        Argue for Medium/Long time horizon when evidence supports it.
        Show that complexity is lower than initially assessed.
        Identify concrete mitigation paths from research evidence.
        """
    guides = STEP09_ISSUE_GUIDES
    rules = STEP09_COMMON_RULES
    output_rules = (StartWithOptimisticAnalyst,)

    item: str = Field(description="The issue to analyze")


class IssuePessimisticSpec(PromptSpec):
    """Argue the pessimistic case for a risk issue in the intermediate review debate."""

    input_documents = STEP09_DOCUMENTS
    role = SeniorVCAnalyst
    task = """\
        Present the PESSIMISTIC perspective on this issue.
        Argue for Short time horizon when evidence supports it.
        Show that impact is higher than initially assessed.
        Identify why standard mitigations may be insufficient.
        """
    guides = STEP09_ISSUE_GUIDES
    rules = STEP09_COMMON_RULES
    output_rules = (StartWithPessimisticAnalyst,)

    item: str = Field(description="The issue to analyze")


# =============================================================================
# 8. PromptSpec — Text output with output_structure
# =============================================================================


class IssueSummarySpec(PromptSpec):
    """Synthesize optimistic and pessimistic arguments into a final issue assessment."""

    input_documents = STEP09_DOCUMENTS
    role = ResearchSupervisor
    task = """\
        Synthesize the debate arguments into a balanced final assessment.
        Weigh evidence from both sides fairly.
        Provide a clear recommendation with confidence level.
        """
    guides = (RiskAssessmentFramework,)
    rules = (CiteEvidence, NoSpeculation)
    output_rules = (DontUseMarkdownTables,)
    output_structure = """\
        ## 1. Executive Summary
        ## 2. Issue Profile
        ### 2.1 Time Horizon Assessment
        ### 2.2 Likelihood Assessment
        ### 2.3 Impact Assessment
        ### 2.4 Mitigation Complexity
        ## 3. Recommendations
        """

    item: str = Field(description="The issue to analyze")
    project_name: str = Field(description="Project name for context")


# =============================================================================
# 9. PromptSpec — Structured output (BaseModel)
# =============================================================================


class RiskVerdict(BaseModel):
    """Structured risk assessment result."""

    time_horizon: str
    likelihood: str
    impact_severity: str
    mitigation_complexity: str
    overall_risk_level: str
    recommendation: str


class IssueVerdictSpec(PromptSpec[RiskVerdict]):
    """Produce a structured risk verdict for a single issue."""

    input_documents = STEP09_DOCUMENTS
    role = ResearchSupervisor
    task = """\
        Based on the debate synthesis, produce a structured risk verdict.
        Assess each dimension of the risk assessment framework.
        """
    guides = (RiskAssessmentFramework,)
    rules = (NoSpeculation,)

    item: str = Field(description="The issue to assess")


# =============================================================================
# 10. PromptSpec — Minimal spec (no optional fields, no dynamic inputs)
# =============================================================================


class WarmupAcknowledgementSpec(PromptSpec):
    """Acknowledgement prompt to populate cache before parallel debate calls."""

    input_documents = STEP09_DOCUMENTS
    role = SeniorVCAnalyst
    task = "Acknowledge that you have read and understood the provided documents."


# =============================================================================
# 11. PromptSpec — Follow-up spec (follows=)
# =============================================================================


class DraftReportSpec(PromptSpec):
    """Draft a research report section based on analysis findings."""

    input_documents = (IntermediateResearchDocument,)
    role = ResearchSupervisor
    task = """\
        Draft the specified section of the research report.
        Use findings from the intermediate research as the primary source.
        """
    rules = (CiteEvidence,)

    section_title: str = Field(description="Report section to draft")
    key_findings: str = Field(description="Bullet points of key findings to incorporate")


class DraftRevisionSpec(PromptSpec, follows=DraftReportSpec):
    """Revise a previously drafted report section based on feedback."""

    task = """\
        Revise the previously drafted section incorporating the provided feedback.
        Maintain the same structure and evidence standards.
        """

    feedback: str = Field(description="Revision feedback to incorporate")


# =============================================================================
# 12. PromptSpec — output_structure with auto-extraction
# =============================================================================


class FinalVerdictSpec(PromptSpec):
    """Produce a final go/no-go verdict with structured output for extraction."""

    input_documents = STEP09_DOCUMENTS
    role = ResearchSupervisor
    task = """\
        Based on all available evidence, produce a final verdict
        on whether this project should proceed to the next stage.
        """
    rules = (CiteEvidence, NoSpeculation)
    output_rules = (DontUseMarkdownTables,)
    output_structure = """\
        ## Verdict
        ## Key Evidence
        ## Risk Summary
        ## Recommendation
        """

    project_name: str = Field(description="Project name")


# =============================================================================
# Demo helpers
# =============================================================================


def _build_prompt_spec_class(
    name: str,
    *,
    attrs: dict[str, Any],
    bases: tuple[Any, ...] = (PromptSpec,),
    kwds: dict[str, Any] | None = None,
) -> type[Any]:
    """Create a PromptSpec subclass dynamically so validation errors are still demonstrated."""

    def _exec_body(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = __name__
        namespace.update(attrs)

    return new_class(name, bases, kwds or {}, _exec_body)


def _capture_validation_error(errors: list[tuple[str, str]], label: str, build: Callable[[], object]) -> None:
    """Append the validation error raised while defining a PromptSpec subclass."""
    try:
        build()
    except TypeError as error:
        errors.append((label, str(error)))


# =============================================================================
# Demo execution
# =============================================================================


def _show_validation_examples() -> None:
    """Demonstrate import-time validation errors (extracted to reduce branch count)."""
    errors: list[tuple[str, str]] = []

    # Missing docstring
    _capture_validation_error(
        errors,
        "Missing docstring",
        lambda: _build_prompt_spec_class(
            "NoDocSpec",
            attrs={
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "do it",
            },
        ),
    )

    # Missing role (standalone spec)
    _capture_validation_error(
        errors,
        "Missing role",
        lambda: _build_prompt_spec_class(
            "NoRoleSpec",
            attrs={
                "__doc__": "Test.",
                "input_documents": (),
                "task": "do it",
            },
        ),
    )

    # Bare field without Field(description=...)
    _capture_validation_error(
        errors,
        "Bare field (no description)",
        lambda: _build_prompt_spec_class(
            "BareFieldSpec",
            attrs={
                "__doc__": "Test.",
                "__annotations__": {"item": str},
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "do it",
            },
        ),
    )

    # OutputRule in rules (wrong tuple)
    _capture_validation_error(
        errors,
        "OutputRule in rules",
        lambda: _build_prompt_spec_class(
            "WrongRuleSpec",
            attrs={
                "__doc__": "Test.",
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "do it",
                "rules": (StartWithOptimisticAnalyst,),
            },
        ),
    )

    # output_structure with BaseModel output_type
    _capture_validation_error(
        errors,
        "output_structure with BaseModel",
        lambda: _build_prompt_spec_class(
            "BadStructSpec",
            bases=(PromptSpec[RiskVerdict],),
            attrs={
                "__doc__": "Test.",
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "do it",
                "output_structure": "## Section",
            },
        ),
    )

    # Empty task
    _capture_validation_error(
        errors,
        "Empty task",
        lambda: _build_prompt_spec_class(
            "EmptyTaskSpec",
            attrs={
                "__doc__": "Test.",
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "   ",
            },
        ),
    )

    # follows must be a PromptSpec subclass
    _capture_validation_error(
        errors,
        "follows=str",
        lambda: _build_prompt_spec_class(
            "BadFollowsSpec",
            kwds={"follows": str},
            attrs={
                "__doc__": "Test.",
                "task": "do it",
            },
        ),
    )

    # follows must not be PromptSpec itself
    _capture_validation_error(
        errors,
        "follows=PromptSpec",
        lambda: _build_prompt_spec_class(
            "FollowsBaseSpec",
            kwds={"follows": PromptSpec},
            attrs={
                "__doc__": "Test.",
                "task": "do it",
            },
        ),
    )

    for label, msg in errors:
        print(f"  [{label}]")
        print(f"    {msg}\n")


def main() -> None:
    print("=" * 80)
    print("PROMPT COMPILER SHOWCASE")
    print("=" * 80)

    # --- Feature: render_text with dynamic values ---
    print("\n--- 1. render_text(): Full prompt with dynamic values ---\n")
    spec = IssueOptimisticSpec(item="Token liquidity: Low trading volume across major DEXs")
    text = render_text(spec)
    print(text)

    # --- Feature: render_preview with placeholders ---
    print("\n" + "=" * 80)
    print("\n--- 2. render_preview(): Prompt template with placeholders ---\n")
    preview = render_preview(IssueSummarySpec)
    print(preview)

    # --- Feature: render_text without document listing (multi-turn follow-up) ---
    print("\n" + "=" * 80)
    print("\n--- 3. render_text(include_input_documents=False): Follow-up turn ---\n")
    followup = IssuePessimisticSpec(item="Token liquidity: Low trading volume across major DEXs")
    text_no_docs = render_text(followup, include_input_documents=False)
    print(text_no_docs)

    # --- Feature: Structured output spec ---
    print("\n" + "=" * 80)
    print("\n--- 4. Structured output spec (output_type=BaseModel) ---\n")
    verdict_spec = IssueVerdictSpec(item="Smart contract upgrade authority unclear")
    verdict_text = render_text(verdict_spec)
    print(verdict_text)
    print(f"\n[output_type = {IssueVerdictSpec._output_type.__name__}]")
    print("[Conversation.send_spec() would use send_structured() automatically]")

    # --- Feature: Minimal spec with no dynamic fields ---
    print("\n" + "=" * 80)
    print("\n--- 5. Minimal spec (no dynamic fields, no guides, no rules) ---\n")
    warmup_text = render_text(WarmupAcknowledgementSpec())
    print(warmup_text)

    # --- Feature: Multiple dynamic fields + output_structure ---
    print("\n" + "=" * 80)
    print("\n--- 6. Multiple dynamic fields + output_structure ---\n")
    summary = IssueSummarySpec(
        item="Token liquidity: Low trading volume across major DEXs",
        project_name="DeFi Bridge Protocol",
    )
    summary_text = render_text(summary)
    print(summary_text)

    # --- Feature: Follow-up spec (follows=) ---
    print("\n" + "=" * 80)
    print("\n--- 7. Follow-up spec (follows=DraftReportSpec) ---\n")
    revision = DraftRevisionSpec(feedback="Add more quantitative evidence and specific metrics.")
    print(render_text(revision))
    follows_spec = DraftRevisionSpec._follows
    if follows_spec is None:
        raise RuntimeError("DraftRevisionSpec should define follows=DraftReportSpec.")
    print(f"\n[follows = {follows_spec.__name__}]")
    print(f"[role = {DraftRevisionSpec.role}]")  # None — inherited from conversation

    # --- Feature: render_preview without documents ---
    print("\n" + "=" * 80)
    print("\n--- 8. render_preview(include_input_documents=False) ---\n")
    print(render_preview(IssueOptimisticSpec, include_input_documents=False))

    # --- Feature: Shared configuration reuse ---
    print("\n" + "=" * 80)
    print("\n--- 9. Shared configuration across specs ---\n")
    print(f"IssueOptimisticSpec.guides  = {tuple(g.__name__ for g in IssueOptimisticSpec.guides)}")
    print(f"IssuePessimisticSpec.guides = {tuple(g.__name__ for g in IssuePessimisticSpec.guides)}")
    print(f"IssueSummarySpec.guides     = {tuple(g.__name__ for g in IssueSummarySpec.guides)}")
    print("\nAll issue specs share STEP09_DOCUMENTS and STEP09_ISSUE_GUIDES via module-level tuples.")

    # --- Feature: render_preview shows follows note ---
    print("\n" + "=" * 80)
    print("\n--- 10. render_preview() shows follows note ---\n")
    preview = render_preview(DraftRevisionSpec)
    print(preview)

    # --- Feature: Import-time validation ---
    print("\n" + "=" * 80)
    print("\n--- 11. Import-time validation examples ---\n")
    _show_validation_examples()

    # --- Feature: Guide.render() reads file content ---
    print("=" * 80)
    print("\n--- 12. Guide.render() — file content ---\n")
    print(f"RiskAssessmentFramework template: {RiskAssessmentFramework.template}")
    content = RiskAssessmentFramework.render()
    print(f"Content ({len(content)} chars, first 3 lines):")
    for line in content.strip().splitlines()[:DISPLAY_PREVIEW_LINE_COUNT]:
        print(f"  {line}")

    # --- Feature: send_spec usage (shown, not executed — requires LLM) ---
    print("\n" + "=" * 80)
    print("\n--- 13. Conversation.send_spec() usage pattern (not executed) ---\n")
    print("""\
    conv = Conversation(model="gemini-3-flash")
    conv = await conv.send_spec(
        IssueOptimisticSpec(item="Token liquidity: Low volume"),
        documents=[whitepaper, research1, research2],
    )
    print(conv.content)  # text response

    conv = Conversation(model="gemini-3.1-pro")
    conv = await conv.send_spec(
        IssueVerdictSpec(item="Smart contract authority"),
        documents=docs,
    )
    print(conv.parsed)  # RiskVerdict instance

    conv = await conv.send_spec(
        DraftRevisionSpec(feedback="Add metrics"),
    )
    print(conv.content)  # revised text

    conv = Conversation(model=model)
    warmup = await conv.send_spec(warmup_spec, documents=docs)
    fork1, fork2 = await asyncio.gather(
        warmup.send(render_text(optimistic_spec, include_input_documents=False)),
        warmup.send(render_text(pessimistic_spec, include_input_documents=False)),
    )""")

    # --- Feature: output_structure spec (auto-extracted) ---
    print("\n" + "=" * 80)
    print("\n--- 14. output_structure spec (auto-extraction in send_spec) ---\n")
    verdict = FinalVerdictSpec(project_name="DeFi Bridge Protocol")
    verdict_text = render_text(verdict)
    print(verdict_text)
    print(f"\n[output_structure = {FinalVerdictSpec.output_structure is not None}]")
    print("[Conversation.send_spec() auto-extracts <result> tags — conv.content returns clean text]")

    # --- Feature: output_structure validation ---
    print("\n" + "=" * 80)
    print("\n--- 15. output_structure validation errors ---\n")

    xml_errors: list[tuple[str, str]] = []

    # H1 header in output_structure
    _capture_validation_error(
        xml_errors,
        "H1 in output_structure",
        lambda: _build_prompt_spec_class(
            "H1OutputSpec",
            attrs={
                "__doc__": "Test.",
                "input_documents": (),
                "role": SeniorVCAnalyst,
                "task": "do it",
                "output_structure": "# Bad Header",
            },
        ),
    )

    for label, msg in xml_errors:
        print(f"  [{label}]")
        print(f"    {msg}\n")

    print("=" * 80)
    print("\nAll features demonstrated successfully.")


if __name__ == "__main__":
    main()
