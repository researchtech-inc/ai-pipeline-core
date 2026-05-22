"""Tests for the PromptContract renderer abstraction (Stage B PR 2).

Two concerns are pinned separately so future PRs can evolve them
independently:

- ``build_prompt_render_context`` assembles the typed data view.
- ``DefaultPromptRenderer`` turns the data view into the user message.

The byte-identity test at the end is the regression pin against silent
drift in section ordering, separator characters, or methodology layout.
"""

from typing import ClassVar

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.llm._conversation_runtime import _SUBSTITUTOR_INSTRUCTION
from ai_pipeline_core.prompt_contract import Methodology, PromptContract, ToolAvailability
from ai_pipeline_core.prompt_contract._render import (
    ContractView,
    DefaultPromptRenderer,
    InputView,
    MethodologyFieldView,
    MethodologyView,
    NotationView,
    OutputView,
    PromptRenderContext,
    PromptRenderer,
    ToolView,
    build_prompt_render_context,
    render_input_value,
)


# ---------------------------------------------------------------------------
# Fixtures: representative contracts and methodologies.
# ---------------------------------------------------------------------------


class RenderOutput(FrozenBaseModel):
    """Structured output for renderer tests."""

    answer: str


class RenderMinimalContract(PromptContract[RenderOutput]):
    """Minimal contract: no inputs, no methodologies, no tools."""

    purpose: ClassVar[str] = "produce an answer"
    returns: ClassVar[str] = "RenderOutput with answer"
    success_criteria: ClassVar[str] = "answer is non-empty"


class RenderInputsContract(PromptContract[RenderOutput]):
    """Contract with two dynamic fields to exercise inputs rendering."""

    purpose: ClassVar[str] = "use the inputs"
    returns: ClassVar[str] = "RenderOutput"
    success_criteria: ClassVar[str] = "any"

    topic: str
    count: int


class RenderStepMethodology(Methodology):
    """Step-by-step reasoning guidance for renderer tests."""

    purpose: ClassVar[str] = "decompose then synthesize"
    rubric: ClassVar[str] = "score each step on a 1-5 scale"


class RenderWithMethodologyContract(PromptContract[RenderOutput]):
    """Contract with one attached methodology."""

    purpose: ClassVar[str] = "produce a careful answer"
    returns: ClassVar[str] = "RenderOutput"
    success_criteria: ClassVar[str] = "any"
    methodologies: ClassVar[tuple[type[Methodology], ...]] = (RenderStepMethodology,)


# ---------------------------------------------------------------------------
# View models are FrozenBaseModel subclasses.
# ---------------------------------------------------------------------------


def test_views_are_frozen_base_model_subclasses() -> None:
    for cls in (
        PromptRenderContext,
        ContractView,
        InputView,
        MethodologyFieldView,
        MethodologyView,
        OutputView,
        ToolView,
        NotationView,
    ):
        assert issubclass(cls, FrozenBaseModel), cls.__name__


def test_default_renderer_satisfies_renderer_protocol() -> None:
    """``DefaultPromptRenderer`` is a structural ``PromptRenderer``."""
    renderer = DefaultPromptRenderer()
    assert isinstance(renderer, PromptRenderer)


# ---------------------------------------------------------------------------
# build_prompt_render_context: contract metadata
# ---------------------------------------------------------------------------


def test_build_context_populates_contract_view() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    assert isinstance(context, PromptRenderContext)
    assert context.contract.class_name == "RenderMinimalContract"
    assert context.contract.title == "Render Minimal"
    assert context.contract.purpose == "produce an answer"
    assert context.contract.returns == "RenderOutput with answer"
    assert context.contract.success_criteria == "answer is non-empty"
    assert context.contract.docstring == "Minimal contract: no inputs, no methodologies, no tools."


def test_build_context_minimal_has_empty_sub_views() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    assert context.inputs == {}
    assert context.input_order == ()
    assert context.methodologies == ()
    assert context.tools == ()
    assert context.documents == ()
    assert context.body == ""
    assert context.notation.active is False
    assert context.notation.instruction == ""


def test_build_context_output_view_carries_class_identity() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    assert context.output.class_name == "RenderOutput"
    assert context.output.module == RenderOutput.__module__
    # PR 2 defers schema/fields/has_cited_text population.
    assert context.output.schema_text == ""
    assert context.output.fields == ()
    assert context.output.has_cited_text is False


# ---------------------------------------------------------------------------
# build_prompt_render_context: inputs
# ---------------------------------------------------------------------------


def test_build_context_inputs_preserve_insertion_order() -> None:
    context = build_prompt_render_context(
        RenderInputsContract,
        dynamic_fields={"topic": "x", "count": 3},
    )
    assert context.input_order == ("topic", "count")
    assert list(context.inputs.keys()) == ["topic", "count"]


def test_build_context_input_view_shapes() -> None:
    context = build_prompt_render_context(
        RenderInputsContract,
        dynamic_fields={"topic": "machine learning", "count": 3},
    )
    topic = context.inputs["topic"]
    assert topic.name == "topic"
    assert topic.title == "topic"
    assert topic.kind == "scalar"
    assert topic.type_name == "str"
    assert topic.text == "machine learning"

    count = context.inputs["count"]
    assert count.name == "count"
    assert count.title == "count"
    assert count.kind == "scalar"
    assert count.type_name == "int"
    assert count.text == "3"


def test_build_context_kind_detection() -> None:
    from enum import StrEnum

    class Color(StrEnum):
        RED = "red"

    class Inner(FrozenBaseModel):
        n: int

    class KindContract(PromptContract[RenderOutput]):
        """Kind detection across supported input shapes."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"

    context = build_prompt_render_context(
        KindContract,
        dynamic_fields={
            "scalar": "x",
            "enum_val": Color.RED,
            "structured": Inner(n=1),
            "tuple_of_models": (Inner(n=1), Inner(n=2)),
            "none_val": None,
            "tuple_of_strings": ("a", "b"),
        },
    )
    assert context.inputs["scalar"].kind == "scalar"
    assert context.inputs["enum_val"].kind == "enum"
    assert context.inputs["structured"].kind == "structured"
    assert context.inputs["tuple_of_models"].kind == "structured"
    assert context.inputs["none_val"].kind == "none"
    assert context.inputs["tuple_of_strings"].kind == "scalar"


# ---------------------------------------------------------------------------
# build_prompt_render_context: methodology view
# ---------------------------------------------------------------------------


def test_build_context_methodology_view_populated() -> None:
    context = build_prompt_render_context(RenderWithMethodologyContract, dynamic_fields={})
    assert len(context.methodologies) == 1
    methodology = context.methodologies[0]
    assert methodology.class_name == "RenderStepMethodology"
    assert methodology.title == "Render Step"
    assert methodology.module == RenderStepMethodology.__module__
    assert methodology.purpose == "decompose then synthesize"
    assert methodology.docstring == "Step-by-step reasoning guidance for renderer tests."
    # ``rubric`` is the only public non-purpose string ClassVar.
    assert len(methodology.fields) == 1
    rubric = methodology.fields[0]
    assert rubric.name == "rubric"
    assert rubric.title == "rubric"
    assert rubric.type_name == "str"
    assert rubric.text == "score each step on a 1-5 scale"


def test_build_context_methodology_fields_sorted_by_name() -> None:
    """Multiple public ClassVars surface in attr-name sort order."""

    class AlphaBetaMethodology(Methodology):
        """Two fields to verify sort order."""

        purpose: ClassVar[str] = "p"
        beta: ClassVar[str] = "second"
        alpha: ClassVar[str] = "first"

    class WithAlphaBetaContract(PromptContract[RenderOutput]):
        """Contract for sort-order test."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"
        methodologies: ClassVar[tuple[type[Methodology], ...]] = (AlphaBetaMethodology,)

    context = build_prompt_render_context(WithAlphaBetaContract, dynamic_fields={})
    field_names = [f.name for f in context.methodologies[0].fields]
    assert field_names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# build_prompt_render_context: tools view
# ---------------------------------------------------------------------------


def test_build_context_tools_view_populated() -> None:
    from pydantic import BaseModel, Field

    from ai_pipeline_core.llm.tools import Tool

    class _EchoTool(Tool):
        """Echo input back unchanged."""

        class Input(BaseModel):
            """Echo input."""

            text: str = Field(description="text")

        class Output(BaseModel):
            """Echo output."""

            text: str

        async def run(self, input: Input) -> Output:
            return self.Output(text=input.text)

    class WithToolContract(PromptContract[RenderOutput]):
        """Contract declaring one tool."""

        purpose: ClassVar[str] = "use the echo tool"
        returns: ClassVar[str] = "RenderOutput"
        success_criteria: ClassVar[str] = "any"
        tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(_EchoTool, max_calls=3),)

    context = build_prompt_render_context(WithToolContract, dynamic_fields={})
    assert len(context.tools) == 1
    tool_view = context.tools[0]
    assert tool_view.name == _EchoTool.name
    assert tool_view.class_name == "_EchoTool"
    assert tool_view.max_calls == 3
    assert tool_view.description == "Echo input back unchanged."


# ---------------------------------------------------------------------------
# build_prompt_render_context: notation toggle
# ---------------------------------------------------------------------------


def test_build_context_notation_inactive_by_default() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    assert context.notation.active is False
    assert context.notation.instruction == ""


def test_build_context_notation_active_populates_instruction() -> None:
    context = build_prompt_render_context(
        RenderMinimalContract,
        dynamic_fields={},
        notation_active=True,
    )
    assert context.notation.active is True
    assert context.notation.instruction == _SUBSTITUTOR_INSTRUCTION


# ---------------------------------------------------------------------------
# DefaultPromptRenderer: section assembly
# ---------------------------------------------------------------------------


def test_default_renderer_minimal_layout() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    rendered = DefaultPromptRenderer().render(context)
    expected = (
        "# Purpose\n\nproduce an answer\n\n"
        "# Returns\n\nRenderOutput with answer\n\n"
        "# Success Criteria\n\nanswer is non-empty"
    )
    assert rendered == expected


def test_default_renderer_inputs_section() -> None:
    context = build_prompt_render_context(
        RenderInputsContract,
        dynamic_fields={"topic": "ml", "count": 3},
    )
    rendered = DefaultPromptRenderer().render(context)
    assert rendered.startswith("# Inputs\n\n## topic\n\nml\n\n## count\n\n3\n\n# Purpose")


def test_default_renderer_methodology_section() -> None:
    context = build_prompt_render_context(RenderWithMethodologyContract, dynamic_fields={})
    rendered = DefaultPromptRenderer().render(context)
    assert "# Reference: Render Step" in rendered
    assert "decompose then synthesize" in rendered
    assert "Step-by-step reasoning guidance for renderer tests." in rendered
    assert "## rubric\n\nscore each step on a 1-5 scale" in rendered


def test_default_renderer_notation_section_only_when_active() -> None:
    inactive = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    rendered_inactive = DefaultPromptRenderer().render(inactive)
    assert "# Notation" not in rendered_inactive

    active = build_prompt_render_context(
        RenderMinimalContract,
        dynamic_fields={},
        notation_active=True,
    )
    rendered_active = DefaultPromptRenderer().render(active)
    assert rendered_active.startswith(f"# Notation\n\n{_SUBSTITUTOR_INSTRUCTION}\n\n# Purpose")


def test_default_renderer_omits_instructions_section_when_body_empty() -> None:
    context = build_prompt_render_context(RenderMinimalContract, dynamic_fields={})
    rendered = DefaultPromptRenderer().render(context)
    assert "# Instructions" not in rendered


# ---------------------------------------------------------------------------
# Byte-identity regression pin: hand-written expected string follows the
# legacy assembly rules verbatim. Any drift in separators, header text, or
# section ordering breaks this test.
# ---------------------------------------------------------------------------


def test_default_renderer_byte_identical_to_legacy_layout() -> None:
    context = build_prompt_render_context(
        RenderWithMethodologyContract,
        dynamic_fields={},
        notation_active=True,
    )
    rendered = DefaultPromptRenderer().render(context)
    expected = (
        f"# Notation\n\n{_SUBSTITUTOR_INSTRUCTION}\n\n"
        "# Purpose\n\nproduce a careful answer\n\n"
        "# Returns\n\nRenderOutput\n\n"
        "# Success Criteria\n\nany\n\n"
        "# Reference: Render Step\n\n"
        "decompose then synthesize\n\n"
        "Step-by-step reasoning guidance for renderer tests.\n\n"
        "## rubric\n\nscore each step on a 1-5 scale"
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# ``render_input_value`` is the canonical helper exposed from ``_render``.
# ---------------------------------------------------------------------------


def test_render_input_value_canonical_location() -> None:
    """The helper is importable from ``ai_pipeline_core.prompt_contract._render``."""
    from ai_pipeline_core.prompt_contract import _render as render_mod

    assert render_mod.render_input_value is render_input_value
