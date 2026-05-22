"""Integration tests for smoke-level list structured output behavior."""

import json
from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.spec import PromptSpec
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class CityInfo(BaseModel):
    """Simple city tuple for list output tests."""

    name: str
    country: str


class MathStep(BaseModel):
    """Simple math step schema."""

    step_number: int
    description: str
    result: str


class TagModel(BaseModel):
    """Single-field tag schema."""

    label: str


class SimpleTag(BaseModel):
    """Tag item used for follow-up conversation smoke coverage."""

    label: str
    confidence: float


class Severity(StrEnum):
    """Severity enum for audit findings."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(StrEnum):
    """Category enum for audit findings."""

    SECURITY = "security"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    USABILITY = "usability"


class AffectedComponent(BaseModel):
    """Affected component details."""

    name: str = Field(description="Component or module name.")
    version: str = Field(description="Version string or 'unknown'.")


class Recommendation(BaseModel):
    """Recommended remediation action."""

    action: str = Field(description="What to do.")
    effort_hours: int = Field(description="Estimated hours to fix.")
    priority: int = Field(description="1=highest, 5=lowest.")


class AuditFinding(BaseModel):
    """Nested finding schema used to smoke test list send_spec with documents."""

    title: str = Field(description="Short finding title.")
    description: str = Field(description="Detailed description of the finding.")
    severity: Severity = Field(description="Severity level.")
    category: FindingCategory = Field(description="Finding category.")
    affected_components: list[AffectedComponent] = Field(description="Components affected.")
    recommendations: list[Recommendation] = Field(description="Recommended actions.")
    cvss_score: float = Field(description="CVSS score from 0.0 to 10.0.")
    is_exploitable: bool = Field(description="Whether the finding is currently exploitable.")


class ListRole(Role):
    """Prompt role for list-output tests."""

    text = "concise geography analyst"


class AuditorRole(Role):
    """Prompt role for audit extraction tests."""

    text = "senior security auditor with expertise in vulnerability assessment"


class ListCitiesSpec(PromptSpec[list[CityInfo]]):
    """Return a fixed list of European capitals."""

    input_documents = ()
    role = ListRole
    task = "Return exactly 3 European capital cities with their country names."


class ReportDocument(Document):
    """Audit report source document for send_spec document-context coverage."""


class AuditFindingsSpec(PromptSpec[list[AuditFinding]]):
    """Extract audit findings from the provided report document."""

    input_documents = (ReportDocument,)
    role = AuditorRole
    task = (
        "Analyze the provided audit report and extract every security finding. "
        "Fill in all fields completely, including affected components and recommendations."
    )


class TestLLMListStructured:
    """Recover list structured-output smoke tests."""

    @pytest.mark.asyncio
    async def test_list_output_basic(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """list[BaseModel] output parses into concrete items."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Return exactly 2 cities: Paris (France) and Berlin (Germany).",
            response_format=list[CityInfo],
            purpose=f"list-structured-basic-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, list)
        assert len(conv.parsed) == 2
        assert all(isinstance(item, CityInfo) for item in conv.parsed)
        assert {"paris", "berlin"} <= {item.name.lower() for item in conv.parsed}

    @pytest.mark.asyncio
    async def test_list_content_is_array_json(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """List structured output content is serialized as a raw JSON array."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Return exactly 1 tag with label 'test'.",
            response_format=list[TagModel],
            purpose=f"list-structured-array-json-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        parsed_content = json.loads(conv.content)
        assert isinstance(parsed_content, list)

    @pytest.mark.asyncio
    async def test_list_output_nested_fields(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Nested fields inside list items are populated correctly."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Return 2 math steps: step 1 is 'add 2+3' with result '5'; step 2 is 'multiply by 2' with result '10'.",
            response_format=list[MathStep],
            purpose=f"list-structured-nested-fields-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, list)
        assert len(conv.parsed) == 2
        assert conv.parsed[0].step_number == 1
        assert conv.parsed[1].result == "10"

    @pytest.mark.asyncio
    async def test_send_spec_list_output(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """PromptSpec[list[T]] works through send_spec."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_spec(
            ListCitiesSpec(),
            purpose="list-structured-send-spec",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, list)
        assert len(conv.parsed) == 3
        assert all(isinstance(item, CityInfo) for item in conv.parsed)

    @pytest.mark.asyncio
    async def test_follow_up_after_list_output(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """A normal follow-up turn can reference a prior list structured output."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_structured(
            "Return exactly 3 tags: alpha=0.9, beta=0.7, gamma=0.5.",
            response_format=list[SimpleTag],
            purpose="list-structured-followup-1",
        )
        cost_budget.add(conv)
        assert len(conv.parsed) == 3

        conv = await conv.send(
            "How many items did you just return? Reply with only the number.",
            purpose="list-structured-followup-2",
        )
        cost_budget.add(conv)

        assert "3" in conv.content

    @pytest.mark.asyncio
    async def test_send_spec_list_with_document_context(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """PromptSpec[list[T]] honors explicit document input through send_spec."""
        report_doc = ReportDocument.create_root(
            name="audit-report.md",
            content=(
                "Security Audit Report - 2026-Q1\n\n"
                "Finding 1: SQL Injection in /api/login endpoint.\n"
                "Severity: CRITICAL. Category: security. CVSS: 9.8. Exploitable: Yes.\n"
                "Affected: auth-service v2.1.0\n"
                "Recommendation: Use parameterized queries. Effort: 8 hours. Priority: 1.\n\n"
                "Finding 2: Missing rate limiting on /api/search.\n"
                "Severity: HIGH. Category: security. CVSS: 7.5. Exploitable: Yes.\n"
                "Affected: api-gateway v1.4.2\n"
                "Recommendation: Add rate limiting middleware. Effort: 4 hours. Priority: 2."
            ),
            reason="integration test",
        )

        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_spec(
            AuditFindingsSpec(),
            documents=[report_doc],
            purpose="list-structured-send-spec-doc-context",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, list)
        assert len(conv.parsed) >= 2
        assert all(isinstance(finding, AuditFinding) for finding in conv.parsed)
        for finding in conv.parsed:
            assert finding.title
            assert finding.severity in list(Severity)
            assert finding.category in list(FindingCategory)
            assert finding.affected_components
            assert finding.recommendations
