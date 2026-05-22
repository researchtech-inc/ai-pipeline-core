"""Tests for prompt_contract._contract (PromptContract validation + execute stub)."""

from typing import ClassVar

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.prompt_contract import (
    Methodology,
    PromptContract,
    PromptResult,
    ToolAvailability,
)


class ContractOutput(FrozenBaseModel):
    """Structured output for tests."""

    answer: str


class OtherOutput(FrozenBaseModel):
    """A second output model for inheritance tests."""

    value: int


class SampleMethodology(Methodology):
    """Sample methodology for tests."""

    purpose: ClassVar[str] = "follow these steps"


class OtherMethodology(Methodology):
    """Second methodology for tests."""

    purpose: ClassVar[str] = "follow other steps"


# ---------------------------------------------------------------------------
# Required ClassVars: purpose / returns / success_criteria
# ---------------------------------------------------------------------------


def test_contract_minimal_valid() -> None:
    class MinimalContract(PromptContract[ContractOutput]):
        """A minimal valid contract."""

        purpose = "do the thing"
        returns = "structured answer"
        success_criteria = "answer is non-empty"

    assert MinimalContract.purpose == "do the thing"
    assert MinimalContract.returns == "structured answer"
    assert MinimalContract.success_criteria == "answer is non-empty"
    assert MinimalContract._output_type is ContractOutput
    assert MinimalContract.methodologies == ()
    assert MinimalContract.tools == ()


@pytest.mark.parametrize("missing", ["purpose", "returns", "success_criteria"])
def test_contract_missing_required_classvar(missing: str) -> None:
    body = {
        "__doc__": "Doc.",
        "purpose": "p",
        "returns": "r",
        "success_criteria": "s",
    }
    body.pop(missing)
    with pytest.raises(TypeError, match=f"must define '{missing}'"):
        type(f"Missing{missing.title()}Contract", (PromptContract[ContractOutput],), body)


def test_contract_inherited_classvar_does_not_satisfy() -> None:
    """A child class cannot reuse a parent contract — the inheritance guard fires first.

    This also closes the silent-attribute-wipe escape hatch where a child class
    omitting methodologies/tools would have ``()`` written back onto it.
    """

    class ParentContract(PromptContract[ContractOutput]):
        """Parent."""

        purpose = "parent purpose"
        returns = "parent returns"
        success_criteria = "parent criteria"

    with pytest.raises(TypeError, match="must inherit directly from PromptContract"):

        class ChildContract(ParentContract):  # type: ignore[misc]
            """Child."""

            returns = "child returns"
            success_criteria = "child criteria"


@pytest.mark.parametrize("attr", ["purpose", "returns", "success_criteria"])
def test_contract_required_classvar_must_be_string(attr: str) -> None:
    body = {
        "__doc__": "Doc.",
        "purpose": "p",
        "returns": "r",
        "success_criteria": "s",
    }
    body[attr] = 123
    with pytest.raises(TypeError, match=f"\\.{attr} must be a string"):
        type(f"BadType{attr.title()}Contract", (PromptContract[ContractOutput],), body)


@pytest.mark.parametrize("attr", ["purpose", "returns", "success_criteria"])
def test_contract_required_classvar_must_not_be_empty(attr: str) -> None:
    body = {
        "__doc__": "Doc.",
        "purpose": "p",
        "returns": "r",
        "success_criteria": "s",
    }
    body[attr] = "   "
    with pytest.raises(TypeError, match=f"\\.{attr} must not be empty"):
        type(f"Empty{attr.title()}Contract", (PromptContract[ContractOutput],), body)


# ---------------------------------------------------------------------------
# Naming: subclass must end with "Contract"
# ---------------------------------------------------------------------------


def test_contract_name_must_end_with_contract() -> None:
    with pytest.raises(TypeError, match="name must end with 'Contract'"):

        class Foo(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_name_with_trailing_contract_accepted() -> None:
    class GoodNameContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    assert GoodNameContract.__name__ == "GoodNameContract"


# ---------------------------------------------------------------------------
# Docstring requirement
# ---------------------------------------------------------------------------


def test_contract_requires_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDocContract(PromptContract[ContractOutput]):
            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_rejects_whitespace_only_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class WhitespaceDocContract(PromptContract[ContractOutput]):
            """ """

            purpose = "p"
            returns = "r"
            success_criteria = "s"


# ---------------------------------------------------------------------------
# Generic parameter / _output_type
# ---------------------------------------------------------------------------


def test_contract_requires_generic_parameter() -> None:
    with pytest.raises(TypeError, match="must declare a structured output type"):

        class NoGenericContract(PromptContract):  # type: ignore[type-arg]
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_rejects_non_basemodel_generic_parameter() -> None:
    with pytest.raises(TypeError, match="must be a FrozenBaseModel subclass"):

        class StringContract(PromptContract[str]):  # type: ignore[type-var]
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_rejects_list_generic_parameter() -> None:
    with pytest.raises(TypeError, match="must be a FrozenBaseModel subclass"):

        class ListContract(PromptContract[list[ContractOutput]]):  # type: ignore[type-var]
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_rejects_plain_basemodel_generic_parameter() -> None:
    """A bare ``BaseModel`` subclass (not ``FrozenBaseModel``) must be rejected.

    Output models must be frozen + extra=forbid so the parsed response is immutable
    and rejects unknown keys; this test pins the bound enforced in
    ``_resolve_output_type``.
    """

    class NonFrozenOutput(BaseModel):
        """Plain BaseModel — not frozen, allows extras."""

        answer: str

    with pytest.raises(TypeError, match="must be a FrozenBaseModel subclass"):

        class NonFrozenContract(PromptContract[NonFrozenOutput]):  # type: ignore[type-var]
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"


def test_contract_output_type_from_generic_param() -> None:
    class TypedContract(PromptContract[OtherOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    assert TypedContract._output_type is OtherOutput


# ---------------------------------------------------------------------------
# Methodologies (optional ClassVar)
# ---------------------------------------------------------------------------


def test_contract_methodologies_default_empty_tuple() -> None:
    class NoMethodologiesContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    assert NoMethodologiesContract.methodologies == ()


def test_contract_methodologies_accepts_methodology_classes() -> None:
    class GoodMethodologiesContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"
        methodologies = (SampleMethodology, OtherMethodology)

    assert GoodMethodologiesContract.methodologies == (SampleMethodology, OtherMethodology)


def test_contract_methodologies_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.methodologies must be a tuple"):

        class BadMethodologiesContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            methodologies = [SampleMethodology]  # list, not tuple


def test_contract_methodologies_rejects_non_methodology() -> None:
    with pytest.raises(TypeError, match="non-Methodology class"):

        class BadMethodologiesContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            methodologies = (str,)


def test_contract_methodologies_rejects_duplicates() -> None:
    with pytest.raises(TypeError, match="contains duplicate"):

        class DupMethodologiesContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            methodologies = (SampleMethodology, SampleMethodology)


# ---------------------------------------------------------------------------
# Tools (optional ClassVar)
# ---------------------------------------------------------------------------


def test_contract_tools_default_empty_tuple() -> None:
    class NoToolsContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    assert NoToolsContract.tools == ()


def test_contract_tools_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.tools must be a tuple"):

        class BadToolsContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            tools = []


def test_contract_tools_rejects_non_availability() -> None:
    with pytest.raises(TypeError, match="ToolAvailability"):

        class BadToolsContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            tools = ("not an availability",)  # type: ignore[assignment]


def test_contract_tools_accepts_availability_values() -> None:
    from ai_pipeline_core.llm.tools import Tool

    class EchoTool(Tool):
        """Echo input."""

        class Input(BaseModel):
            """Echo input."""

            text: str = Field(description="text")

        class Output(BaseModel):
            """Echo output."""

            text: str

        async def run(self, input: Input) -> Output:
            return self.Output(text=input.text)

    class WithToolsContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"
        tools = (ToolAvailability(EchoTool, max_calls=2),)

    assert len(WithToolsContract.tools) == 1
    assert WithToolsContract.tools[0].tool is EchoTool


def test_contract_tools_rejects_duplicate_tool_classes() -> None:
    from ai_pipeline_core.llm.tools import Tool

    class EchoTool(Tool):
        """Echo input."""

        class Input(BaseModel):
            """Echo input."""

            text: str = Field(description="text")

        class Output(BaseModel):
            """Echo output."""

            text: str

        async def run(self, input: Input) -> Output:
            return self.Output(text=input.text)

    with pytest.raises(TypeError, match="contains duplicate"):

        class DupToolsContract(PromptContract[ContractOutput]):
            """Doc."""

            purpose = "p"
            returns = "r"
            success_criteria = "s"
            tools = (ToolAvailability(EchoTool, max_calls=2), ToolAvailability(EchoTool, max_calls=3))


# ---------------------------------------------------------------------------
# Pydantic instance fields
# ---------------------------------------------------------------------------


def test_contract_with_instance_fields() -> None:
    class FieldedContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

        topic: str = Field(description="topic to analyze")

    instance = FieldedContract(topic="dynamics")
    assert instance.topic == "dynamics"


def test_contract_frozen_instance() -> None:
    class FrozenContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

        topic: str = Field(description="topic")

    from pydantic import ValidationError

    instance = FrozenContract(topic="x")
    with pytest.raises(ValidationError):
        instance.topic = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# execute() signature: still exposes (model, *, tool_bindings) keyword shape
# (behavioral tests of execute() live in tests/unit/prompt_contract/test_execute.py).
# ---------------------------------------------------------------------------


def test_execute_signature() -> None:
    """execute() takes (self, model, *, tool_bindings=()) and returns PromptResult."""
    import inspect

    class SigContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    sig = inspect.signature(SigContract.execute)
    params = list(sig.parameters.values())
    assert params[0].name == "self"
    assert params[1].name == "model"
    assert params[1].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[2].name == "tool_bindings"
    assert params[2].kind == inspect.Parameter.KEYWORD_ONLY
    assert params[2].default == ()


# ---------------------------------------------------------------------------
# PromptResult / DocumentCitation
# ---------------------------------------------------------------------------


def test_prompt_result_holds_response_and_citations() -> None:
    from ai_pipeline_core import FrozenBaseModel
    from ai_pipeline_core.prompt_contract import DocumentCitation

    result: PromptResult[ContractOutput] = PromptResult(
        response=ContractOutput(answer="42"),
        citations=(),
    )
    assert result.response.answer == "42"
    assert result.citations == ()
    # Stage A: DocumentCitation is a real FrozenBaseModel (no longer aliases _llm_core.Citation)
    assert issubclass(DocumentCitation, FrozenBaseModel)


# ---------------------------------------------------------------------------
# Regression pin: required ClassVars must use ClassVar annotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attr", ["purpose", "returns", "success_criteria"])
def test_contract_rejects_required_attr_annotated_as_pydantic_field(attr: str) -> None:
    """A bare ``purpose: str = "..."`` declaration must be rejected — Pydantic would
    consume it as an instance field, stripping the class attribute and breaking
    every downstream reader that expects ``type(contract).{attr}``.
    """
    body: dict[str, object] = {
        "__doc__": "Doc.",
        "__annotations__": dict.fromkeys(("purpose", "returns", "success_criteria"), str),
        "purpose": "p",
        "returns": "r",
        "success_criteria": "s",
    }
    # Force the offending attribute back to a plain annotation; the other two
    # stay annotated as str too so the error fires on the parametrized attr first.
    body["__annotations__"] = {attr: str, **{k: ClassVar[str] for k in _OTHER_REQUIRED_ATTRS(attr)}}
    with pytest.raises(TypeError, match=f"\\.{attr} must be declared as 'ClassVar"):
        type(f"Bad{attr.title()}Contract", (PromptContract[ContractOutput],), body)


def _OTHER_REQUIRED_ATTRS(attr: str) -> tuple[str, ...]:
    return tuple(a for a in ("purpose", "returns", "success_criteria") if a != attr)


def test_contract_accepts_required_attr_with_classvar_annotation() -> None:
    """The supported form: explicit ClassVar annotation keeps the value as a class attribute."""

    class GoodAnnContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"

    # The values survive on the CLASS object after Pydantic processing —
    # this is what PR 4 render code relies on.
    assert GoodAnnContract.purpose == "p"
    assert GoodAnnContract.returns == "r"
    assert GoodAnnContract.success_criteria == "s"
    # And nothing leaks into Pydantic instance fields.
    assert "purpose" not in GoodAnnContract.model_fields
    assert "returns" not in GoodAnnContract.model_fields
    assert "success_criteria" not in GoodAnnContract.model_fields


def test_contract_accepts_required_attr_without_annotation() -> None:
    """No annotation at all is also fine — the value lives on the class as a plain attribute."""

    class PlainAttrContract(PromptContract[ContractOutput]):
        """Doc."""

        purpose = "p"
        returns = "r"
        success_criteria = "s"

    assert PlainAttrContract.purpose == "p"
    assert "purpose" not in PlainAttrContract.model_fields


# ---------------------------------------------------------------------------
# Regression pin: direct-inheritance guard
# ---------------------------------------------------------------------------


def test_contract_rejects_multi_level_inheritance() -> None:
    """A subclass of an existing PromptContract subclass must be rejected.

    Without the guard, a child class that omits ``methodologies``/``tools``
    would have ``()`` silently written back over the parent's value, and
    generic-arg extraction can fail on deeper chains.
    """

    class FirstContract(PromptContract[ContractOutput]):
        """First."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"

    with pytest.raises(TypeError, match="must inherit directly from PromptContract"):

        class SecondContract(FirstContract):  # type: ignore[misc]
            """Second."""

            purpose: ClassVar[str] = "p2"
            returns: ClassVar[str] = "r2"
            success_criteria: ClassVar[str] = "s2"


def test_contract_inheritance_guard_protects_methodologies_from_silent_wipe() -> None:
    """Concretely demonstrate the silent-wipe scenario: a child class that omits
    methodologies would, without the guard, have ``parent.methodologies`` overwritten
    with ``()``. The guard rejects the child outright, closing the escape hatch.
    """

    class ParentSampleMethodology(Methodology):
        """Sample."""

        purpose: ClassVar[str] = "x"

    class ParentWithMethodologyContract(PromptContract[ContractOutput]):
        """Parent with methodology."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"
        methodologies = (ParentSampleMethodology,)

    assert ParentWithMethodologyContract.methodologies == (ParentSampleMethodology,)

    with pytest.raises(TypeError, match="must inherit directly from PromptContract"):

        class ChildOmittingMethodologyContract(ParentWithMethodologyContract):  # type: ignore[misc]
            """Child omits methodologies."""

            purpose: ClassVar[str] = "p"
            returns: ClassVar[str] = "r"
            success_criteria: ClassVar[str] = "s"

    # Parent is untouched after the failed child attempt.
    assert ParentWithMethodologyContract.methodologies == (ParentSampleMethodology,)


def test_contract_rejects_multiple_bases() -> None:
    """Mixing PromptContract with another base is also disallowed by the guard."""

    class Mixin:
        """Mixin."""

    with pytest.raises(TypeError, match="must inherit directly from PromptContract"):

        class MultiBaseContract(PromptContract[ContractOutput], Mixin):  # type: ignore[misc]
            """Multi-base."""

            purpose: ClassVar[str] = "p"
            returns: ClassVar[str] = "r"
            success_criteria: ClassVar[str] = "s"
