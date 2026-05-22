"""Tests for prompt_compiler.spec (PromptSpec validation)."""

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.documents import Document
from ai_pipeline_core.prompt_compiler.components import Guide, OutputRule, Role, Rule
from ai_pipeline_core.prompt_compiler.spec import (
    MultiLineField,
    PromptSpec,
    _check_field_descriptions,
    _check_no_duplicates,
    _check_unknown_attrs,
)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class SpecDoc(Document):
    """Spec input document."""


class SpecDoc2(Document):
    """Second input document."""


class SpecRole(Role):
    """Spec role."""

    text = "experienced reviewer"


class SpecRule(Rule):
    """Spec rule."""

    text = "Use only provided evidence"


class SpecRule2(Rule):
    """Second rule."""

    text = "Be concise"


class SpecOutputRule(OutputRule):
    """Spec output rule."""

    text = "Return concise bullet points"


class SpecOutputRule2(OutputRule):
    """Second output rule."""

    text = "Use markdown headers"


class SpecPayload(BaseModel):
    """Structured output model."""

    answer: str


@pytest.fixture
def temp_modules() -> Generator[list[str]]:
    created: list[str] = []
    yield created
    for module_name in created:
        sys.modules.pop(module_name, None)


def _make_guide(tmp_path: Path, temp_modules: list[str], *, class_name: str = "SpecGuide") -> type[Guide]:
    module_name = f"spec_guide_mod_{uuid4().hex}"
    module = ModuleType(module_name)
    module.__file__ = str(tmp_path / f"{module_name}.py")
    sys.modules[module_name] = module
    temp_modules.append(module_name)

    file_path = tmp_path / "guide.txt"
    if not file_path.exists():
        file_path.write_text("Guide body\n", encoding="utf-8")
    return type(class_name, (Guide,), {"__module__": module_name, "__doc__": "Guide doc.", "template": "guide.txt"})


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_check_no_duplicates_accepts_unique_tuple() -> None:
    class A: ...

    class B: ...

    _check_no_duplicates((A, B), attr="rules", spec_name="S")


def test_check_no_duplicates_rejects_duplicates() -> None:
    class A: ...

    with pytest.raises(TypeError, match="contains duplicate: A"):
        _check_no_duplicates((A, A), attr="rules", spec_name="S")


def test_check_unknown_attrs_rejects_plain_unknown_value() -> None:
    class Dummy:
        extra = 1

    with pytest.raises(TypeError, match="has unknown attribute 'extra'"):
        _check_unknown_attrs(Dummy, "Dummy")


def test_check_unknown_attrs_ignores_private_attrs() -> None:
    class Dummy:
        _private = "ok"

    _check_unknown_attrs(Dummy, "Dummy")


def test_check_unknown_attrs_ignores_callables_and_descriptors() -> None:
    class Dummy:
        __annotations__ = {"field": str}
        field = Field(description="Field")

        def method(self) -> str:
            return "ok"

        @classmethod
        def cls_method(cls) -> str:
            return "ok"

        @staticmethod
        def static_method() -> str:
            return "ok"

        @property
        def prop(self) -> str:
            return "ok"

    _check_unknown_attrs(Dummy, "Dummy")


def test_check_field_descriptions_skips_known_spec_attrs() -> None:
    class Dummy:
        __annotations__ = {"task": str}

    _check_field_descriptions(Dummy, "Dummy")


def test_check_field_descriptions_rejects_bare_annotation() -> None:
    class Dummy:
        __annotations__ = {"item": str}

    with pytest.raises(TypeError, match=r"Bare 'item: \.\.\.' is not allowed"):
        _check_field_descriptions(Dummy, "Dummy")


def test_check_field_descriptions_rejects_field_without_description() -> None:
    class Dummy:
        __annotations__ = {"item": str}
        item = Field()

    with pytest.raises(TypeError, match="Bare Field\\(\\) without description is not allowed"):
        _check_field_descriptions(Dummy, "Dummy")


def test_check_field_descriptions_accepts_described_field() -> None:
    class Dummy:
        __annotations__ = {"item": str}
        item = Field(description="Item")

    _check_field_descriptions(Dummy, "Dummy")


def test_check_field_descriptions_accepts_declared_forward_ref_and_ignores_inherited_fields() -> None:
    class Parent:
        inherited = Field(description="Inherited")
        __annotations__ = {"inherited": str}

    class Dummy(Parent):
        item: ForwardDeclared = Field(description="Item")

    class ForwardDeclared:
        pass

    _check_field_descriptions(Dummy, "Dummy")


def test_check_field_descriptions_no_annotations() -> None:
    class Dummy:
        pass

    _check_field_descriptions(Dummy, "Dummy")


# ---------------------------------------------------------------------------
# PromptSpec validation: inheritance
# ---------------------------------------------------------------------------


def test_spec_requires_direct_inheritance() -> None:
    class BaseSpec(PromptSpec):
        """Base."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    with pytest.raises(TypeError, match="must inherit directly from PromptSpec"):

        class ChildSpec(BaseSpec):
            """Child."""

            input_documents = ()
            role = SpecRole
            task = "do it"


# ---------------------------------------------------------------------------
# PromptSpec validation: docstring
# ---------------------------------------------------------------------------


def test_spec_requires_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDocSpec(PromptSpec):
            input_documents = ()
            role = SpecRole
            task = "do it"


def test_spec_rejects_whitespace_only_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class WhitespaceDocSpec(PromptSpec):
            """ """

            input_documents = ()
            role = SpecRole
            task = "do it"


# ---------------------------------------------------------------------------
# PromptSpec validation: follows
# ---------------------------------------------------------------------------


def test_follows_basic() -> None:
    """Follow-up spec with just task works."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        task = "continue"

    assert FollowSpec._follows is InitSpec
    assert FollowSpec.role is None
    assert FollowSpec.input_documents == ()


def test_follows_with_role() -> None:
    """Follow-up can override role."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        role = SpecRole
        task = "continue"

    assert FollowSpec.role is SpecRole


def test_follows_with_input_documents() -> None:
    """Follow-up can declare additional input_documents."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        input_documents = (SpecDoc,)
        task = "continue"

    assert FollowSpec.input_documents == (SpecDoc,)


def test_follows_must_be_prompt_spec_subclass() -> None:
    with pytest.raises(TypeError, match="must be a PromptSpec subclass"):

        class BadSpec(PromptSpec, follows=str):  # type: ignore[arg-type]  # negative test: wrong runtime type
            """Bad."""

            task = "do it"


def test_follows_must_not_be_prompt_spec_itself() -> None:
    with pytest.raises(TypeError, match="concrete PromptSpec subclass"):

        class BadSpec(PromptSpec, follows=PromptSpec):
            """Bad."""

            task = "do it"


def test_follows_task_still_required() -> None:
    """Task is required even for follow-ups."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    with pytest.raises(TypeError, match="must define 'task'"):

        class BadFollow(PromptSpec, follows=InitSpec):
            """Bad."""


def test_standalone_spec_requires_role() -> None:
    """Non-follow-up specs must have role."""
    with pytest.raises(TypeError, match="must define 'role'"):

        class BadSpec(PromptSpec):
            """Bad."""

            input_documents = ()
            task = "do it"


def test_standalone_spec_requires_input_documents() -> None:
    """Non-follow-up specs must have input_documents."""
    with pytest.raises(TypeError, match="must define 'input_documents'"):

        class BadSpec(PromptSpec):
            """Bad."""

            role = SpecRole
            task = "do it"


# ---------------------------------------------------------------------------
# PromptSpec validation: role
# ---------------------------------------------------------------------------


def test_spec_requires_role() -> None:
    with pytest.raises(TypeError, match="must define 'role'"):

        class NoRoleSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            task = "do it"


def test_spec_role_must_be_role_subclass() -> None:
    with pytest.raises(TypeError, match=r"\.role must be a Role subclass"):

        class BadRoleSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = "not a class"
            task = "do it"


def test_spec_role_rejects_non_role_class() -> None:
    with pytest.raises(TypeError, match=r"\.role must be a Role subclass"):

        class BadRoleSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = str
            task = "do it"


# ---------------------------------------------------------------------------
# PromptSpec validation: task
# ---------------------------------------------------------------------------


def test_spec_requires_task() -> None:
    with pytest.raises(TypeError, match="must define 'task'"):

        class NoTaskSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole


def test_spec_task_must_be_string() -> None:
    with pytest.raises(TypeError, match=r"\.task must be a string"):

        class BadTaskSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = 123


def test_spec_task_is_dedented_and_stripped() -> None:
    class NormalizedSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = """
            Line one
            Line two
        """

    assert NormalizedSpec.task == "Line one\nLine two"


def test_spec_task_must_not_be_empty() -> None:
    with pytest.raises(TypeError, match=r"\.task must not be empty"):

        class EmptyTaskSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "   "


# ---------------------------------------------------------------------------
# PromptSpec validation: input_documents
# ---------------------------------------------------------------------------


def test_spec_requires_input_documents() -> None:
    with pytest.raises(TypeError, match="must define 'input_documents'"):

        class NoDocsSpec(PromptSpec):
            """Doc."""

            role = SpecRole
            task = "do it"


def test_spec_input_documents_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.input_documents must be a tuple"):

        class ListDocsSpec(PromptSpec):
            """Doc."""

            input_documents = [SpecDoc]
            role = SpecRole
            task = "do it"


def test_spec_input_documents_must_contain_document_subclasses() -> None:
    with pytest.raises(TypeError, match="contains non-Document class"):

        class BadDocsSpec(PromptSpec):
            """Doc."""

            input_documents = (str,)
            role = SpecRole
            task = "do it"


def test_spec_input_documents_no_duplicates() -> None:
    with pytest.raises(TypeError, match=r"\.input_documents contains duplicate"):

        class DupDocsSpec(PromptSpec):
            """Doc."""

            input_documents = (SpecDoc, SpecDoc)
            role = SpecRole
            task = "do it"


def test_spec_input_documents_empty_tuple_allowed() -> None:
    class EmptyDocsSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert EmptyDocsSpec.input_documents == ()


# ---------------------------------------------------------------------------
# PromptSpec validation: output_type
# ---------------------------------------------------------------------------


def test_spec_output_type_defaults_to_str() -> None:
    class DefaultOutputSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert DefaultOutputSpec._output_type is str


def test_spec_output_type_explicit_str() -> None:
    class ExplicitStrSpec(PromptSpec[str]):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert ExplicitStrSpec._output_type is str


def test_spec_output_type_rejects_invalid_generic_param() -> None:
    with pytest.raises(TypeError, match=r"generic parameter must be 'str', a BaseModel subclass, or list\[BaseModel\]"):

        class BadOutputSpec(PromptSpec[int]):  # type: ignore[type-arg]  # negative test: missing/invalid type argument
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"


def test_spec_output_type_rejects_manual_declaration() -> None:
    # Pydantic 2.13+ rejects non-annotated class attributes via PydanticUserError
    # (a RuntimeError subclass) at metaclass __new__ time, before our
    # __init_subclass__ TypeError check can fire. Either rejection is acceptable
    # — both surface 'output_type' as the offending name.
    with pytest.raises((TypeError, RuntimeError), match="output_type"):

        class ManualOutputSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"
            output_type = str


def test_spec_output_type_basemodel_via_generic() -> None:
    class ModelOutputSpec(PromptSpec[SpecPayload]):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert ModelOutputSpec._output_type is SpecPayload


# ---------------------------------------------------------------------------
# PromptSpec validation: guides
# ---------------------------------------------------------------------------


def test_spec_guides_default_to_empty_tuple() -> None:
    class DefaultGuidesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert DefaultGuidesSpec.guides == ()


def test_spec_guides_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.guides must be a tuple"):

        class BadGuidesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            guides = []


def test_spec_guides_must_contain_guide_subclasses() -> None:
    with pytest.raises(TypeError, match="contains non-Guide class"):

        class WrongGuidesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            guides = (SpecRole,)


def test_spec_guides_no_duplicates(tmp_path: Path, temp_modules: list[str]) -> None:
    guide_cls = _make_guide(tmp_path, temp_modules, class_name="DupGuide")
    with pytest.raises(TypeError, match=r"\.guides contains duplicate"):

        class DupGuidesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            guides = (guide_cls, guide_cls)


# ---------------------------------------------------------------------------
# PromptSpec validation: rules
# ---------------------------------------------------------------------------


def test_spec_rules_default_to_empty_tuple() -> None:
    class DefaultRulesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert DefaultRulesSpec.rules == ()


def test_spec_rules_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.rules must be a tuple"):

        class BadRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            rules = []


@pytest.mark.ai_docs
def test_spec_rules_reject_output_rule_with_specific_message() -> None:
    from ai_pipeline_core.prompt_compiler import OutputRule, PromptSpec, Role

    class ReviewerRole(Role):
        """Reviewer."""

        text = "careful reviewer"

    class FormatBullets(OutputRule):
        """Bullet formatting."""

        text = "Return concise bullet points"

    with pytest.raises(TypeError, match=r"\.rules contains OutputRule 'FormatBullets'"):

        class MixedSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = ReviewerRole
            task = "do it"

            rules = (FormatBullets,)  # Wrong! Should be output_rules=


def test_spec_rules_reject_non_rule_class() -> None:
    with pytest.raises(TypeError, match="contains non-Rule class"):

        class WrongRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            rules = (SpecRole,)


def test_spec_rules_no_duplicates() -> None:
    with pytest.raises(TypeError, match=r"\.rules contains duplicate"):

        class DupRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            rules = (SpecRule, SpecRule)


# ---------------------------------------------------------------------------
# PromptSpec validation: output_rules
# ---------------------------------------------------------------------------


def test_spec_output_rules_default_to_empty_tuple() -> None:
    class DefaultOutputRulesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert DefaultOutputRulesSpec.output_rules == ()


def test_spec_output_rules_must_be_tuple() -> None:
    with pytest.raises(TypeError, match=r"\.output_rules must be a tuple"):

        class BadOutputRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_rules = []


def test_spec_output_rules_reject_rule_with_specific_message() -> None:
    with pytest.raises(TypeError, match=r"\.output_rules contains Rule 'SpecRule'"):

        class MixedSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_rules = (SpecRule,)


def test_spec_output_rules_reject_non_output_rule_class() -> None:
    with pytest.raises(TypeError, match="contains non-OutputRule class"):

        class WrongOutputRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_rules = (SpecRole,)


def test_spec_output_rules_no_duplicates() -> None:
    with pytest.raises(TypeError, match=r"\.output_rules contains duplicate"):

        class DupOutputRulesSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_rules = (SpecOutputRule, SpecOutputRule)


# ---------------------------------------------------------------------------
# PromptSpec validation: output_structure
# ---------------------------------------------------------------------------


def test_spec_output_structure_only_with_str_output() -> None:
    with pytest.raises(TypeError, match=r"\.output_structure is only allowed when output_type is str"):

        class BadStructSpec(PromptSpec[SpecPayload]):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"
            output_structure = "## Heading"


def test_spec_output_structure_must_be_string() -> None:
    with pytest.raises(TypeError, match=r"\.output_structure must be a string"):

        class WrongTypeStructSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_structure = 123


def test_spec_output_structure_must_not_be_empty() -> None:
    with pytest.raises(TypeError, match=r"\.output_structure must not be empty"):

        class EmptyStructSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_structure = "   "


def test_spec_output_structure_rejects_h1_headers() -> None:
    with pytest.raises(TypeError, match=r"must not contain H1 headers"):

        class H1StructSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            output_structure = "# Forbidden\n## Allowed"


def test_spec_output_structure_allows_h2() -> None:
    class OkStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

        output_structure = "## Section\n### Subsection"

    assert OkStructSpec.output_structure == "## Section\n### Subsection"


def test_spec_output_structure_is_dedented_and_stripped() -> None:
    class NormalizedStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

        output_structure = """
            ## Section A
            ### Detail
        """

    assert NormalizedStructSpec.output_structure == "## Section A\n### Detail"


def test_spec_output_structure_default_none() -> None:
    class DefaultStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

    assert DefaultStructSpec.output_structure is None


def test_output_structure_with_basemodel_raises() -> None:
    """output_structure is forbidden with structured output (BaseModel)."""
    with pytest.raises(TypeError, match="output_structure is only allowed"):

        class BadSpec(PromptSpec[SpecPayload]):
            """Bad."""

            input_documents = ()
            role = SpecRole
            task = "do it"
            output_structure = "## Section"


def test_output_structure_with_xml_output_rule_raises() -> None:
    """OutputRule with XML tags + output_structure raises."""

    class XmlRule(OutputRule):
        """XML rule."""

        text = "Wrap in <document> tags"

    with pytest.raises(TypeError, match="references XML tags"):

        class BadSpec(PromptSpec):
            """Bad."""

            input_documents = ()
            role = SpecRole
            task = "do it"
            output_structure = "## Section"
            output_rules = (XmlRule,)


def test_output_structure_allows_output_rule_without_xml_tags() -> None:
    class PlainOutputRule(OutputRule):
        """No XML."""

        text = "Return concise bullet points"

    class OkSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"
        output_structure = "## Section"
        output_rules = (PlainOutputRule,)

    assert OkSpec.output_structure is not None


# ---------------------------------------------------------------------------
# PromptSpec validation: Pydantic fields
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_spec_bare_field_no_description() -> None:
    from ai_pipeline_core.prompt_compiler import PromptSpec, Role

    class ReviewerRole(Role):
        """Reviewer."""

        text = "careful reviewer"

    with pytest.raises(TypeError, match=r"field 'item' must use Field\(description='\.\.\.'\)"):

        class BareFieldSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = ReviewerRole
            task = "do it"

            item: str  # Wrong! Must use Field(description='...')


def test_spec_field_without_description() -> None:
    with pytest.raises(TypeError, match="Bare Field\\(\\) without description is not allowed"):

        class NoDescFieldSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            item: str = Field()


def test_spec_field_with_description() -> None:
    class OkFieldSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

        item: str = Field(description="Item to process")

    spec = OkFieldSpec(item="x")
    assert spec.item == "x"


# ---------------------------------------------------------------------------
# PromptSpec validation: unknown attributes
# ---------------------------------------------------------------------------


def test_spec_rejects_unknown_attributes() -> None:
    """Pydantic intercepts non-annotated attrs before __init_subclass__.

    Tuple values like `ruels = ()` are caught by Pydantic as non-annotated attrs.
    Non-tuple values that Pydantic doesn't catch are caught by _check_unknown_attrs.
    """
    from pydantic import PydanticUserError

    with pytest.raises(PydanticUserError, match="non-annotated attribute was detected: `ruels"):

        class TypoSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"

            ruels = ()


def test_spec_allows_methods_and_descriptors() -> None:
    class HelperSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

        item: str = Field(description="Item")

        def method(self) -> str:
            return self.item

        @classmethod
        def build(cls, item: str) -> HelperSpec:
            return cls(item=item)

        @staticmethod
        def normalize(value: str) -> str:
            return value.strip()

        @property
        def upper(self) -> str:
            return self.item.upper()

    spec = HelperSpec(item="abc")
    assert spec.method() == "abc"
    assert spec.upper == "ABC"


# ---------------------------------------------------------------------------
# PromptSpec: full valid spec
# ---------------------------------------------------------------------------


def test_spec_full_valid(tmp_path: Path, temp_modules: list[str]) -> None:
    guide_cls = _make_guide(tmp_path, temp_modules)

    class FullSpec(PromptSpec):
        """A complete spec with all features."""

        input_documents = (SpecDoc, SpecDoc2)
        role = SpecRole
        task = "Analyze input"
        guides = (guide_cls,)
        rules = (SpecRule, SpecRule2)
        output_rules = (SpecOutputRule,)

        output_structure = "## Summary\n## Details"
        item: str = Field(description="The item to analyze")

    assert FullSpec._follows is None
    assert FullSpec.role is SpecRole
    assert FullSpec.task == "Analyze input"
    assert FullSpec.output_structure == "## Summary\n## Details"
    spec = FullSpec(item="test")
    assert spec.item == "test"


def test_spec_frozen_model() -> None:
    class FrozenSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "do it"

        item: str = Field(description="Item")

    spec = FrozenSpec(item="x")
    with pytest.raises(Exception):
        spec.item = "y"  # type: ignore[misc]  # frozen model mutation negative test


# ---------------------------------------------------------------------------
# Task field placeholder detection
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_task_field_placeholder_single_field() -> None:
    """Task referencing a single field via {field_name} should raise."""
    with pytest.raises(TypeError, match=r"task contains field placeholder references.*\{topic\}"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = 'Analyze the "{topic}" for key findings.'  # Wrong! task is rendered literally, not as a template
            topic: str = Field(description="Research topic")


def test_task_field_placeholder_multiple_fields() -> None:
    """Task referencing multiple fields should list all conflicts."""
    with pytest.raises(TypeError, match=r"task contains field placeholder references.*\{item\}.*\{scope\}"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "Evaluate {item} within {scope}."
            item: str = Field(description="The item")
            scope: str = Field(description="The scope")


def test_task_field_placeholder_multiline_field() -> None:
    """Task referencing a MultiLineField should also be caught."""
    with pytest.raises(TypeError, match=r"task contains field placeholder references.*\{feedback\}"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "Review the document and apply the {feedback}."
            feedback: str = MultiLineField(description="Review feedback")


def test_task_braces_not_matching_field_allowed() -> None:
    """{identifier} in task that does NOT match a declared field should be fine."""

    class OkSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "Return JSON like {key: value} or use {some_other_thing} in your response."
        item: str = Field(description="The item")

    assert OkSpec.task


def test_task_field_name_without_braces_allowed() -> None:
    """Field name mentioned in task as plain text (no braces) should be fine."""

    class OkSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "Use the topic provided in context to classify findings."
        topic: str = Field(description="Research topic")

    assert OkSpec.task


def test_task_no_fields_with_braces_allowed() -> None:
    """Spec with no dynamic fields should allow any braces in task."""

    class OkSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SpecRole
        task = "Return output as {key: value} pairs."

    assert OkSpec.task


def test_task_placeholder_error_explains_framework_rendering() -> None:
    """Error message should explain how the framework renders fields."""
    with pytest.raises(TypeError, match=r"rendered literally") as exc_info:

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "Classify {category}."
            category: str = Field(description="Category")

    msg = str(exc_info.value)
    assert "# Context" in msg
    assert "MultiLineField" in msg
    assert "Remove {field_name} references" in msg
    assert "Example fix" in msg
