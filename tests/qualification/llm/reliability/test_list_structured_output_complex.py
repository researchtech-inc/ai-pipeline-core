"""Qualification lane: broad list structured-output reliability across catalog models, slow paths, and stress shapes."""

import json
from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core._degeneration import detect_output_degeneration
from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


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
    """Nested finding schema for structured list reliability."""

    title: str = Field(description="Short finding title.")
    description: str = Field(description="Detailed description of the finding.")
    severity: Severity = Field(description="Severity level.")
    category: FindingCategory = Field(description="Finding category.")
    affected_components: list[AffectedComponent] = Field(description="Components affected.")
    recommendations: list[Recommendation] = Field(description="Recommended actions.")
    cvss_score: float = Field(description="CVSS score from 0.0 to 10.0.")
    is_exploitable: bool = Field(description="Whether the finding is currently exploitable.")


class InventoryItem(BaseModel):
    """Item with optional fields to stress list output richness."""

    name: str = Field(description="Item name.")
    sku: str = Field(description="Stock keeping unit.")
    price_usd: float = Field(description="Price in USD.")
    description: str | None = Field(default=None, description="Optional description.")
    tags: list[str] = Field(default_factory=list, description="Optional tags.")
    in_stock: bool = Field(default=True, description="Whether in stock.")


class TextSnippet(BaseModel):
    """Text snippet with arbitrary Unicode content."""

    source: str = Field(description="Source identifier.")
    text: str = Field(description="The text content.")


class LabelItem(BaseModel):
    """Single-field label item."""

    label: str = Field(description="A text label.")


class ColorItem(BaseModel):
    """Color schema for consecutive list-output testing."""

    name: str = Field(description="Color name.")
    hex_code: str = Field(description="Hex color code like #FF0000.")


class ShapeItem(BaseModel):
    """Shape schema for consecutive list-output testing."""

    name: str = Field(description="Shape name.")
    sides: int = Field(description="Number of sides, or 0 for circle.")


class TestListStructuredOutputComplex:
    """Recover slow-path and stress-shape list structured-output reliability tests."""

    @pytest.mark.asyncio
    async def test_complex_list_output_not_flagged_as_degeneration(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Deeply nested list structured output must not trip degeneration detection."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Generate exactly 5 security audit findings for a web application. "
            "Finding 1: SQL injection in login (critical, security, CVSS 9.8, exploitable). "
            "Finding 2: Missing rate limiting on API (high, security, CVSS 7.5, exploitable). "
            "Finding 3: Slow database queries on search page (medium, performance, CVSS 4.0, not exploitable). "
            "Finding 4: No CSRF tokens on forms (high, security, CVSS 8.1, exploitable). "
            "Finding 5: Missing alt text on images (low, usability, CVSS 0.0, not exploitable). "
            "Each finding must have at least one affected component and one recommendation.",
            response_format=list[AuditFinding],
            purpose=f"qualification-list-degeneration-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, list)
        assert len(conv.parsed) == 5
        assert isinstance(json.loads(conv.content), list)
        assert detect_output_degeneration(conv.content) is None

    @pytest.mark.asyncio
    async def test_ten_item_rich_list(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Large, fully-populated list output stays well-formed."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Generate exactly 10 inventory items for a hardware store. "
            "Use the names Hammer, Screwdriver, Wrench, Pliers, Drill, Saw, Level, Tape Measure, Sandpaper, and Paint Brush. "
            "Each item must include a realistic SKU, price between 5 and 200 USD, a 1-2 sentence description, "
            "at least 2 tags, and a realistic in_stock value.",
            response_format=list[InventoryItem],
            purpose=f"qualification-list-rich-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert len(conv.parsed) == 10
        assert all(isinstance(item, InventoryItem) for item in conv.parsed)
        for item in conv.parsed:
            assert item.name
            assert item.sku
            assert item.price_usd > 0
        assert len(json.loads(conv.content)) == 10

    @pytest.mark.asyncio
    async def test_unicode_in_fields(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Unicode content survives JSON and Pydantic round-trips."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Return exactly 3 text snippets with non-ASCII characters: "
            "one French snippet with accented letters, one symbols snippet with currency or arrows, "
            "and one CJK snippet with Chinese or Japanese characters.",
            response_format=list[TextSnippet],
            purpose=f"qualification-list-unicode-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert len(conv.parsed) == 3
        assert isinstance(json.loads(conv.content), list)
        all_text = " ".join(snippet.text for snippet in conv.parsed)
        assert any(ord(char) > 127 for char in all_text)

    @pytest.mark.asyncio
    async def test_duplicate_items_not_deduped(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Duplicate list items are preserved rather than silently deduplicated."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_structured(
            "Return exactly 4 label items. The first two must both have label='duplicate'. The third has label='unique_a'. The fourth has label='unique_b'.",
            response_format=list[LabelItem],
            purpose="qualification-list-duplicates",
        )

        cost_budget.add(conv)
        labels = [item.label for item in conv.parsed]
        assert len(labels) == 4
        assert labels.count("duplicate") == 2

    @pytest.mark.asyncio
    async def test_two_consecutive_list_outputs(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Back-to-back list structured outputs on one conversation both parse correctly."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_structured(
            "Return exactly 2 colors: Red (#FF0000) and Blue (#0000FF).",
            response_format=list[ColorItem],
            purpose="qualification-list-consecutive-1",
        )
        cost_budget.add(conv)
        assert len(conv.parsed) == 2

        conv = await conv.send_structured(
            "Now return exactly 2 shapes: Triangle (3 sides) and Circle (0 sides).",
            response_format=list[ShapeItem],
            purpose="qualification-list-consecutive-2",
        )
        cost_budget.add(conv)
        assert len(conv.parsed) == 2
        assert any(shape.sides == 3 for shape in conv.parsed)

    @pytest.mark.asyncio
    async def test_enum_fields_in_list_items(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Enum and boolean fields in list items keep the expected runtime types."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send_structured(
            "Return exactly 3 audit findings: "
            "1. title='XSS in search', severity=high, category=security, CVSS=8.0, exploitable=true, "
            "component search-ui v3.0, recommendation sanitize input (4h, priority 1). "
            "2. title='Slow queries', severity=medium, category=performance, CVSS=4.0, exploitable=false, "
            "component db-service v2.1, recommendation add indexes (8h, priority 2). "
            "3. title='Poor contrast', severity=low, category=usability, CVSS=0.0, exploitable=false, "
            "component frontend v1.5, recommendation update CSS (2h, priority 3).",
            response_format=list[AuditFinding],
            purpose=f"qualification-list-enums-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert len(conv.parsed) == 3
        for finding in conv.parsed:
            assert isinstance(finding.severity, Severity)
            assert isinstance(finding.category, FindingCategory)
            assert isinstance(finding.is_exploitable, bool)
            assert 0.0 <= finding.cvss_score <= 10.0
