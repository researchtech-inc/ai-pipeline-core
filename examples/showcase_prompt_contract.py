#!/usr/bin/env python3
"""PromptContract showcase: structured output, tools, and declared follow-ups.

This example demonstrates the typed ``PromptContract`` surface end to end:

- An opener contract (:class:`DraftBriefContract`) turns a topic into a draft
  research brief.
- A follow-up contract (:class:`FinalizeBriefContract`) declares
  ``continues=Continues.once(DraftBriefContract)`` so it continues the opener's
  conversation, receives the opener's typed result through a
  ``PromptResult``-typed field, and is allowed to call a bounded tool
  (:class:`FactLookupTool`) to enrich the final brief.

The contracts are exempt from the paired ``.j2`` body-file requirement because
they live under ``examples/``; the framework renders the contract framing
(purpose / returns / success criteria / inputs / tools) directly.

It is safe to run without an LLM proxy: ``smoke()`` only inspects the declared
wiring. A real two-step exchange runs only when both an ``OPENAI_BASE_URL`` is
configured and ``RUN_PROMPT_CONTRACT_SHOWCASE=1`` is set.

Usage::

    python examples/showcase_prompt_contract.py
    RUN_PROMPT_CONTRACT_SHOWCASE=1 python examples/showcase_prompt_contract.py
"""

import asyncio
import os
from typing import ClassVar

from pydantic import BaseModel, Field

from ai_pipeline_core import (
    AIModel,
    Continues,
    FrozenBaseModel,
    PromptContract,
    PromptResult,
    Tool,
    ToolAvailability,
)
from ai_pipeline_core.settings import settings

# A tiny in-memory registry the tool reads from. In a real application this
# would be a service client, database, or search index bound at execute() time.
_FACT_REGISTRY: dict[str, str] = {
    "tardigrades": "Tardigrades can survive the vacuum of space for several days.",
    "honey": "Honey never spoils; edible honey has been found in ancient tombs.",
    "octopus": "An octopus has three hearts and blue, copper-based blood.",
}


class FactLookupTool(Tool):
    """Look up a verified fact for a topic from the curated fact registry."""

    class Input(BaseModel):
        """Lookup input."""

        topic: str = Field(description="Lower-case topic keyword to look up.")

    class Output(BaseModel):
        """Lookup output."""

        fact: str = Field(description="A verified fact, or a not-found message.")

    async def run(self, input: Input) -> Output:
        fact = _FACT_REGISTRY.get(input.topic.strip().lower())
        return self.Output(fact=fact or f"No verified fact on file for '{input.topic}'.")


class BriefDraft(FrozenBaseModel):
    """First-pass research brief produced by the opener."""

    summary: str = Field(description="One-paragraph draft summary of the topic.")
    open_questions: tuple[str, ...] = Field(description="Questions a fact lookup should resolve.")


class FinalBrief(FrozenBaseModel):
    """Finalized research brief produced by the follow-up."""

    summary: str = Field(description="Revised summary incorporating the looked-up fact.")
    verified_fact: str = Field(description="The verified fact the final brief relied on.")


class DraftBriefContract(PromptContract[BriefDraft]):
    """Draft a short research brief for a topic and list its open questions."""

    purpose: ClassVar[str] = "Draft a research brief and surface questions a fact lookup could resolve."
    returns: ClassVar[str] = "A BriefDraft with a summary and a tuple of open questions."
    success_criteria: ClassVar[str] = "summary is non-empty and at least one open question is listed."

    topic: str = Field(description="Topic to research.")


class FinalizeBriefContract(
    PromptContract[FinalBrief],
    continues=Continues.once(DraftBriefContract),
):
    """Finalize the drafted brief in the same conversation using a fact lookup."""

    purpose: ClassVar[str] = "Resolve an open question with the fact tool and finalize the brief."
    returns: ClassVar[str] = "A FinalBrief whose summary incorporates the verified fact."
    success_criteria: ClassVar[str] = "verified_fact is non-empty and the summary reflects it."
    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(FactLookupTool, max_calls=2),)

    prior: PromptResult[BriefDraft] = Field(description="The draft turn this finalization continues.")


def build_model() -> AIModel:
    """Create the AIModel handle used for both turns of the exchange."""
    return AIModel(name="gemini-3-flash")


def describe_wiring() -> None:
    """Print the declared lineage and tool surface without making any calls."""
    print("DraftBriefContract:")
    print(f"  output model : {DraftBriefContract._output_type.__name__}")
    print(f"  inputs       : {tuple(DraftBriefContract.model_fields)}")
    print("FinalizeBriefContract:")
    continues = FinalizeBriefContract._continues
    opener_name = continues.opener.__name__ if continues is not None else "(none)"
    print(f"  continues    : {opener_name}")
    print(f"  predecessor  : {FinalizeBriefContract._predecessor_field_name}")
    print(f"  tools        : {tuple(t.tool.__name__ for t in FinalizeBriefContract.tools)}")


async def maybe_run_live(model: AIModel) -> FinalBrief | None:
    """Run the opener then the declared follow-up, when explicitly enabled."""
    if not settings.openai_base_url:
        print("Skipping live run: OPENAI_BASE_URL is not configured.")
        return None
    if os.environ.get("RUN_PROMPT_CONTRACT_SHOWCASE") != "1":
        print("Skipping live run: set RUN_PROMPT_CONTRACT_SHOWCASE=1 to use the configured AIPL proxy.")
        return None

    draft = await DraftBriefContract(topic="tardigrades").execute(model)
    print("\nDraft summary:", draft.response.summary)
    print("Open questions:", draft.response.open_questions)

    final = await FinalizeBriefContract(prior=draft).execute(
        model,
        tool_bindings=(FactLookupTool.bind(),),
    )
    print("\nFinal summary:", final.response.summary)
    print("Verified fact:", final.response.verified_fact)
    return final.response


async def main() -> None:
    model = build_model()
    describe_wiring()
    await maybe_run_live(model)


def smoke() -> None:
    """Inspect the declared contract wiring without making any live calls."""
    build_model()
    describe_wiring()
    assert DraftBriefContract._output_type is BriefDraft
    assert FinalizeBriefContract._continues is not None
    assert FinalizeBriefContract._continues.opener is DraftBriefContract
    assert FinalizeBriefContract._predecessor_field_name == "prior"
    assert FinalizeBriefContract.tools[0].tool is FactLookupTool
    # Bindings are constructed via Tool.bind(**kwargs), never ToolBinding(...) directly.
    binding = FactLookupTool.bind()
    assert binding.tool is FactLookupTool


if __name__ == "__main__":
    asyncio.run(main())
