"""Integration: PromptContract.execute() end-to-end against a live LLM.

Smoke test for Stage 2 PR 4. Confirms the contract surface dispatches a real
request through the engine, parses the structured response, and returns a
``PromptResult`` carrying both the typed BaseModel and any citations.
"""

from typing import ClassVar

import pytest
from pydantic import Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.llm import AIModel
from ai_pipeline_core.prompt_contract import PromptContract, PromptResult, ValidationFailure
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class CityFact(FrozenBaseModel):
    """A short structured response about a city."""

    name: str = Field(description="Canonical city name.")
    country: str = Field(description="Country name.")


class CityFactContract(PromptContract[CityFact]):
    """Return a CityFact for the requested city."""

    purpose: ClassVar[str] = "Identify the country for the named city."
    returns: ClassVar[str] = "A CityFact object with both name and country populated."
    success_criteria: ClassVar[str] = "name and country are non-empty strings."

    city: str = Field(description="Name of the city to identify.")

    def validate(self, response: CityFact) -> tuple[ValidationFailure, ...]:  # type: ignore[override]
        """Reject empty fields."""
        failures: list[ValidationFailure] = []
        if not response.name.strip():
            failures.append(ValidationFailure(field="name", message="name must not be empty."))
        if not response.country.strip():
            failures.append(ValidationFailure(field="country", message="country must not be empty."))
        return tuple(failures)


class TestPromptContractMinimal:
    """Live-LLM smoke test for PromptContract.execute()."""

    @pytest.mark.asyncio
    async def test_execute_returns_structured_prompt_result(
        self,
        default_test_model: AIModel,
    ) -> None:
        """A minimal contract returns a PromptResult[CityFact] from the live LLM."""
        result = await CityFactContract(city="Paris").execute(default_test_model)

        assert isinstance(result, PromptResult)
        assert isinstance(result.response, CityFact)
        assert result.response.name.strip()
        assert result.response.country.strip()
        # The response carries citations (possibly empty).
        assert isinstance(result.citations, tuple)
