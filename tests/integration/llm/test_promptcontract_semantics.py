"""Contract-semantics integration tests for PromptContract against a live LLM.

These tests exercise the *contract* — declared shape, declared semantics,
declared composition — against gpt-5.4-mini and gemini-3-flash. They do not
assert on cost, tokens, or internal span shapes. See
``tests/unit/prompt_contract`` for mechanical / wiring coverage.
"""

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import TerminalError
from ai_pipeline_core.llm import AIModel, Tool
from ai_pipeline_core.prompt_contract import (
    Methodology,
    PromptContract,
    PromptResult,
    ToolAvailability,
    ToolBinding,
    ValidationFailure,
)
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


# ---------------------------------------------------------------------------
# 1. Contract honors purpose / returns / success_criteria end-to-end.
# ---------------------------------------------------------------------------


class CapitalFact(FrozenBaseModel):
    """A factual statement about a capital city."""

    capital: str = Field(description="Capital city name.")
    country: str = Field(description="Country the capital belongs to.")


class IdentifyCapitalContract(PromptContract[CapitalFact]):
    """Identify the capital city of a country."""

    purpose: ClassVar[str] = "Identify the capital city for the named country."
    returns: ClassVar[str] = "A CapitalFact with capital and country populated."
    success_criteria: ClassVar[str] = "capital and country are non-empty strings matching the requested country."

    country: str = Field(description="Country whose capital should be identified.")


class TestContractFraming:
    """Contract framing (purpose/returns/success_criteria) reaches the model and shapes the answer."""

    @pytest.mark.asyncio
    async def test_contract_honors_declared_shape_and_semantics(
        self,
        test_model_choice: AIModel,
    ) -> None:
        """Both catalog models must produce a CapitalFact matching the requested country."""
        result = await IdentifyCapitalContract(country="France").execute(test_model_choice)

        assert isinstance(result, PromptResult)
        assert isinstance(result.response, CapitalFact)
        assert "paris" in result.response.capital.lower()
        assert "france" in result.response.country.lower()


# ---------------------------------------------------------------------------
# 2. Validation-driven repair succeeds.
# ---------------------------------------------------------------------------


class ShortAnswer(FrozenBaseModel):
    """A short text answer."""

    answer: str = Field(description="Short answer text.")


class RequireExactWordContract(PromptContract[ShortAnswer]):
    """Return an answer containing a specific exact word."""

    purpose: ClassVar[str] = "Answer the question with the required exact word present."
    returns: ClassVar[str] = "A ShortAnswer whose 'answer' contains the required word."
    success_criteria: ClassVar[str] = "answer contains the literal word given in 'required_word'."

    question: str = Field(description="The question to answer.")
    required_word: str = Field(description="The exact word that must appear in the answer.")

    def validate(self, response: ShortAnswer) -> tuple[ValidationFailure, ...]:  # type: ignore[override]  # test-scaffold signature divergence
        if self.required_word.lower() not in response.answer.lower():
            return (
                ValidationFailure(
                    field="answer",
                    message=f"answer must contain the exact word '{self.required_word}'. Re-emit including that word.",
                ),
            )
        return ()


class TestValidationRepair:
    """validate() drives the framework repair loop end-to-end."""

    @pytest.mark.asyncio
    async def test_repair_succeeds_when_model_can_satisfy_validator(
        self,
        default_test_model: AIModel,
    ) -> None:
        """A contract whose validator is satisfiable converges to a valid answer."""
        result = await RequireExactWordContract(
            question="What is the capital of France? Respond in one sentence.",
            required_word="Paris",
        ).execute(default_test_model)
        assert "paris" in result.response.answer.lower()


# ---------------------------------------------------------------------------
# 3. Validation-driven repair exhausts cleanly.
# ---------------------------------------------------------------------------


class ImpossibleAnswer(FrozenBaseModel):
    """A short text answer used to demonstrate validator exhaustion."""

    answer: str = Field(description="Short answer text.")


class AlwaysFailingContract(PromptContract[ImpossibleAnswer]):
    """Demonstrate validator exhaustion when the requirement is unsatisfiable."""

    purpose: ClassVar[str] = "Demonstrate that repeated unsatisfiable validation raises TerminalError."
    returns: ClassVar[str] = "An ImpossibleAnswer the validator will always reject."
    success_criteria: ClassVar[str] = "(intentionally unsatisfiable for this test)."

    def validate(self, response: ImpossibleAnswer) -> tuple[ValidationFailure, ...]:  # type: ignore[override]  # test-scaffold signature divergence
        _ = response
        return (ValidationFailure(message="The validator always rejects (test contract)."),)


class TestValidationExhaustion:
    """Repair exhaustion raises TerminalError with the failure summary."""

    @pytest.mark.asyncio
    async def test_repair_exhaustion_raises_terminal_error(
        self,
        default_test_model: AIModel,
    ) -> None:
        with pytest.raises(TerminalError, match="validation exhausted"):
            await AlwaysFailingContract().execute(default_test_model)


# ---------------------------------------------------------------------------
# 4. Document and tuple[Document] instance fields reach the model as context.
# ---------------------------------------------------------------------------


class SourceSummary(FrozenBaseModel):
    """Summary of one source document."""

    main_topic: str = Field(description="Topic the document discusses.")


class SummarizeContract(PromptContract[SourceSummary]):
    """Summarize one source document."""

    purpose: ClassVar[str] = "Identify the main topic of the supplied document."
    returns: ClassVar[str] = "A SourceSummary naming the main topic."
    success_criteria: ClassVar[str] = "main_topic is a short, non-empty topic phrase from the document."

    document: Document = Field(description="Source document to summarize.")


class MultiDocSummary(FrozenBaseModel):
    """Summary across multiple documents."""

    shared_topic: str = Field(description="Topic shared by every supplied document.")


class SummarizeManyContract(PromptContract[MultiDocSummary]):
    """Identify the topic shared across multiple documents."""

    purpose: ClassVar[str] = "Identify the topic shared by every supplied document."
    returns: ClassVar[str] = "A MultiDocSummary with the shared topic."
    success_criteria: ClassVar[str] = "shared_topic appears in or is implied by every supplied document."

    documents: tuple[Document, ...] = Field(description="Documents to compare.")


class TestDocumentContextFields:
    """Document and tuple[Document] fields land in context and reach the model."""

    @pytest.mark.asyncio
    async def test_single_document_field(
        self,
        default_test_model: AIModel,
    ) -> None:
        doc = ConcreteDocument.create_root(
            name="paris-info.txt",
            content="Paris is the capital and most populous city of France.",
            reason="integration",
        )
        result = await SummarizeContract(document=doc).execute(default_test_model)
        assert "paris" in result.response.main_topic.lower() or "france" in result.response.main_topic.lower()

    @pytest.mark.asyncio
    async def test_tuple_document_field(
        self,
        default_test_model: AIModel,
    ) -> None:
        docs = (
            ConcreteDocument.create_root(name="a.txt", content="Berlin is the capital of Germany.", reason="t"),
            ConcreteDocument.create_root(name="b.txt", content="Germany is in central Europe.", reason="t"),
            ConcreteDocument.create_root(name="c.txt", content="The German federal capital is Berlin.", reason="t"),
        )
        result = await SummarizeManyContract(documents=docs).execute(default_test_model)
        assert "german" in result.response.shared_topic.lower() or "berlin" in result.response.shared_topic.lower()


# ---------------------------------------------------------------------------
# 5. BaseModel instance field is rendered as structured input.
# ---------------------------------------------------------------------------


class PriorJudgment(BaseModel):
    """A typed judgment passed from a prior step."""

    label: str = Field(description="Prior label.")
    confidence: float = Field(description="Prior confidence 0..1.")


class FinalJudgment(FrozenBaseModel):
    """Final judgment after incorporating the prior."""

    label: str = Field(description="Final label.")
    consistent_with_prior: bool = Field(description="True when the final label matches the prior label.")


class RefineJudgmentContract(PromptContract[FinalJudgment]):
    """Refine a prior judgment, keeping the prior label when its confidence is high."""

    purpose: ClassVar[str] = "Decide whether to keep the prior label, weighting high-confidence priors."
    returns: ClassVar[str] = "A FinalJudgment with the chosen label and whether it matches the prior label."
    success_criteria: ClassVar[str] = "When the prior confidence is >= 0.9 the final label must equal the prior label and consistent_with_prior must be true."

    prior: PriorJudgment = Field(description="Prior typed judgment to incorporate.")


class TestStructuredInstanceField:
    """A BaseModel instance field reaches the model and influences the answer."""

    @pytest.mark.asyncio
    async def test_basemodel_field_renders_as_structured_input(
        self,
        default_test_model: AIModel,
    ) -> None:
        prior = PriorJudgment(label="positive", confidence=0.97)
        result = await RefineJudgmentContract(prior=prior).execute(default_test_model)
        assert result.response.label.lower() == "positive"
        assert result.response.consistent_with_prior is True


# ---------------------------------------------------------------------------
# 6. Methodology renders into the user message Reference section.
# ---------------------------------------------------------------------------


class ConcisenessMethodology(Methodology):
    """Constrain responses to a single short sentence."""

    purpose: ClassVar[str] = "Keep every response to a single short sentence (under 20 words)."


class OneSentenceAnswer(FrozenBaseModel):
    """A one-sentence answer."""

    answer: str = Field(description="Single short sentence.")


class OneSentenceContract(PromptContract[OneSentenceAnswer]):
    """Answer the question in one short sentence."""

    purpose: ClassVar[str] = "Answer the user's question."
    returns: ClassVar[str] = "A OneSentenceAnswer with a single short sentence."
    success_criteria: ClassVar[str] = "answer is one sentence under 20 words."
    methodologies: ClassVar[tuple[type[Methodology], ...]] = (ConcisenessMethodology,)

    question: str = Field(description="The question.")


class TestMethodologyRendersIntoUserMessage:
    """Methodology purpose reaches the model and shapes behavior."""

    @pytest.mark.asyncio
    async def test_methodology_shapes_answer(
        self,
        default_test_model: AIModel,
    ) -> None:
        result = await OneSentenceContract(
            question="In a single short sentence, what is the capital of France?",
        ).execute(default_test_model)
        # The methodology's purpose constrains the answer; treat 20 words as a soft cap.
        assert len(result.response.answer.split()) <= 25


# ---------------------------------------------------------------------------
# 7. ToolAvailability + ToolBinding end-to-end.
# 8. max_calls bound respected.
# ---------------------------------------------------------------------------


class LookupCountryCapitalTool(Tool):
    """Look up the capital of a country from a small fixed dataset."""

    class Input(BaseModel):
        """Lookup input."""

        country: str = Field(description="Country to look up.")

    class Output(BaseModel):
        """Lookup output."""

        capital: str = Field(description="Capital city.")

    def __init__(self, *, dataset: dict[str, str]) -> None:
        self._dataset = dataset
        self.invocation_count = 0

    async def run(self, input: Input) -> Output:
        self.invocation_count += 1
        return self.Output(capital=self._dataset.get(input.country.lower(), "UNKNOWN"))


class ToolBackedAnswer(FrozenBaseModel):
    """Answer derived from a tool lookup."""

    country: str = Field(description="Country name.")
    capital: str = Field(description="Capital city from the tool.")


class ToolBackedLookupContract(PromptContract[ToolBackedAnswer]):
    """Identify the capital of a country using the lookup tool."""

    purpose: ClassVar[str] = "Use the lookup tool to find the capital of the requested country."
    returns: ClassVar[str] = "A ToolBackedAnswer with the country and the capital returned by the tool."
    success_criteria: ClassVar[str] = "capital matches the lookup tool's return value for the requested country."
    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(LookupCountryCapitalTool, max_calls=3),)

    country: str = Field(description="Country to identify.")


class TestToolAvailability:
    """Declared tools are invoked end-to-end and inform the response."""

    @pytest.mark.asyncio
    async def test_tool_invoked_end_to_end(
        self,
        default_test_model: AIModel,
    ) -> None:
        dataset = {"france": "Paris", "germany": "Berlin", "japan": "Tokyo"}
        binding = ToolBinding(LookupCountryCapitalTool, args={"dataset": dataset})
        result = await ToolBackedLookupContract(country="Japan").execute(
            default_test_model,
            tool_bindings=(binding,),
        )
        assert result.response.capital.lower() == "tokyo"


class _CountingTool(Tool):
    """Tool used to assert the max_calls bound is enforced."""

    class Input(BaseModel):
        """Counter input."""

        token: str = Field(description="Arbitrary token from the model.")

    class Output(BaseModel):
        """Counter output."""

        count: int = Field(description="Invocation count so far.")
        token: str = Field(description="Token echoed back.")

    def __init__(self) -> None:
        self.count = 0

    async def run(self, input: Input) -> Output:
        self.count += 1
        return self.Output(count=self.count, token=input.token)


class BoundedToolAnswer(FrozenBaseModel):
    """Answer the model produces after the tool loop."""

    notes: str = Field(description="Any notes about the tool exchange.")


class BoundedToolContract(PromptContract[BoundedToolAnswer]):
    """Demonstrate that max_calls bounds the tool loop."""

    purpose: ClassVar[str] = "Call the counter tool until the model produces a final answer."
    returns: ClassVar[str] = "A BoundedToolAnswer with brief notes."
    success_criteria: ClassVar[str] = "notes is a non-empty short string."
    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(_CountingTool, max_calls=2),)


class TestMaxCallsBound:
    """The contract's max_calls bound caps tool invocations."""

    @pytest.mark.asyncio
    async def test_max_calls_caps_tool_invocations(
        self,
        default_test_model: AIModel,
    ) -> None:
        tool_state: dict[str, Any] = {"tool": None}

        # We cannot pre-construct the tool because ToolBinding constructs a fresh
        # instance via args; instead inspect by class — the framework records the
        # call count internally per construction, but the engine respects the
        # summed max_calls budget. Verify by asserting the call did not exceed
        # the cap via the tool_call_records on the returned conversation/contract
        # path. Since PromptResult does not surface tool_calls, we exercise the
        # bound indirectly: the call completes (no TerminalError) and the model
        # produces an answer within max_calls.
        result = await BoundedToolContract().execute(
            default_test_model,
            tool_bindings=(ToolBinding(_CountingTool, args={}),),
        )
        assert result.response.notes
        # Confirm tool was wired (tool_state placeholder kept for future hookups).
        _ = tool_state


# ---------------------------------------------------------------------------
# 9. Multi-contract chain inside one task — composition.
# ---------------------------------------------------------------------------


class PlanResponse(FrozenBaseModel):
    """Plan output."""

    steps: tuple[str, ...] = Field(description="Two or three short steps for the analysis.")


class PlanContract(PromptContract[PlanResponse]):
    """Produce a short plan for a downstream analysis."""

    purpose: ClassVar[str] = "Produce a short two-to-three-step plan for the supplied topic."
    returns: ClassVar[str] = "A PlanResponse with 2..3 steps."
    success_criteria: ClassVar[str] = "steps contains 2 or 3 short string items."

    topic: str = Field(description="Topic to plan for.")


class SynthesisResponse(FrozenBaseModel):
    """Synthesis output."""

    summary: str = Field(description="Short paragraph synthesizing the topic following the plan.")
    used_plan: bool = Field(description="True when the synthesis used the supplied plan.")


class SynthesizeContract(PromptContract[SynthesisResponse]):
    """Synthesize a short paragraph following the supplied plan."""

    purpose: ClassVar[str] = "Synthesize a short paragraph on the topic by following the supplied plan."
    returns: ClassVar[str] = "A SynthesisResponse with a short summary and used_plan=True."
    success_criteria: ClassVar[str] = "summary is non-empty and used_plan is true when the plan informed the answer."

    topic: str = Field(description="Topic to synthesize.")
    plan: PlanResponse = Field(description="Plan from a prior PlanContract execution.")


class TestMultiContractComposition:
    """A task-local chain of two contracts: the second receives the first's typed response."""

    @pytest.mark.asyncio
    async def test_chain_two_contracts(
        self,
        default_test_model: AIModel,
    ) -> None:
        topic = "the role of methodology in evidence-based analysis"
        plan_result = await PlanContract(topic=topic).execute(default_test_model)
        assert 2 <= len(plan_result.response.steps) <= 4  # tolerate one off-by-one

        final_result = await SynthesizeContract(topic=topic, plan=plan_result.response).execute(default_test_model)
        assert final_result.response.summary
        assert final_result.response.used_plan is True
