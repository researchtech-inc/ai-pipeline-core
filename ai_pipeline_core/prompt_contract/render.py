"""PromptContract render context and prompt assembly."""

import json
import logging
import re
import typing
from collections.abc import Mapping
from enum import StrEnum
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, get_args, get_origin

import jinja2
from pydantic import BaseModel, Field

from ai_pipeline_core._pydantic_base import FrozenBaseModel
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm._request_assembly import _SUBSTITUTOR_INSTRUCTION

from .cited_text import CitedText
from .methodology import Methodology

__all__ = [
    "CitationsView",
    "ContractView",
    "DocumentRef",
    "InputView",
    "MethodologyFieldView",
    "MethodologyView",
    "NotationView",
    "OutputFieldView",
    "OutputView",
    "PromptRenderContext",
    "ToolView",
    "build_prompt_render_context",
    "render_input_value",
    "render_prompt",
]


logger = logging.getLogger(__name__)


_PASCAL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_PRIMITIVE_TYPES = (str, bool, int, float)


def _pascal_to_title(name: str) -> str:
    return _PASCAL_BOUNDARY_RE.sub(" ", name)


def _field_label(field_name: str) -> str:
    return _pascal_to_title(field_name.replace("_", " ")).strip() or field_name


def render_input_value(value: Any) -> str:  # noqa: PLR0911  # type-dispatched renderer; each branch is a distinct supported field shape
    """Render a non-document contract input value into prompt text."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            return "[]"
        if all(isinstance(item, BaseModel) for item in items):
            return "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in items) + "\n]"
        if all(isinstance(item, _PRIMITIVE_TYPES) for item in items):
            return "\n".join(f"- {render_input_value(item)}" for item in items)
        logger.warning(
            "PromptContract input field collection contains unsupported item types; falling back to repr(): %r. "
            "Use scalar values, enums, FrozenBaseModel instances, or tuples of those supported values.",
            items,
        )
        return repr(items)
    if value is None:
        return "(none)"
    logger.warning(
        "PromptContract input field value has unsupported type %s; falling back to repr(): %r. "
        "Use scalar values, enums, FrozenBaseModel instances, or Document inputs.",
        type(value).__name__,
        value,
    )
    return repr(value)


def _input_kind(value: Any) -> Literal["scalar", "structured", "enum", "none"]:
    if value is None:
        return "none"
    if isinstance(value, StrEnum):
        return "enum"
    if isinstance(value, BaseModel):
        return "structured"
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, BaseModel) for item in value):
        return "structured"
    return "scalar"


def _unwrap_annotated(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        if args:
            return _unwrap_annotated(args[0])
    return annotation


def _is_cited_text_annotation(annotation: Any) -> bool:
    annotation = _unwrap_annotated(annotation)
    if annotation is NoneType:
        return False
    origin = get_origin(annotation)
    if origin in {typing.Union, UnionType}:
        return any(_is_cited_text_annotation(arg) for arg in get_args(annotation))
    return annotation is CitedText


class ContractView(FrozenBaseModel):
    """Contract metadata available to templates."""

    class_name: str
    title: str
    module: str
    purpose: str
    returns: str
    success_criteria: str
    docstring: str | None = None


class InputView(FrozenBaseModel):
    """One non-document instance field rendered in the user prompt."""

    name: str
    title: str
    kind: Literal["scalar", "structured", "enum", "none"]
    type_name: str
    text: str


class DocumentRef(FrozenBaseModel):
    """Reference to one document supplied as model context."""

    field_name: str
    index: int
    name: str
    document_id: str
    class_name: str
    reference: str


class MethodologyFieldView(FrozenBaseModel):
    """One public non-purpose string ClassVar attached to a methodology."""

    name: str
    title: str
    type_name: str
    text: str


class MethodologyView(FrozenBaseModel):
    """One methodology attached to a contract."""

    class_name: str
    title: str
    module: str
    purpose: str = ""
    docstring: str | None = None
    body: str = ""
    fields: tuple[MethodologyFieldView, ...] = ()


class OutputFieldView(FrozenBaseModel):
    """One field on the contract's output model."""

    name: str
    title: str
    type_name: str
    required: bool = True
    is_cited_text: bool = False


class OutputView(FrozenBaseModel):
    """The contract's output model identity and field view."""

    class_name: str
    module: str
    schema_text: str = ""
    fields: tuple[OutputFieldView, ...] = ()
    has_cited_text: bool = False


class ToolView(FrozenBaseModel):
    """One declared tool availability."""

    name: str
    class_name: str
    class_path: str
    description: str | None = None
    max_calls: int


class CitationsView(FrozenBaseModel):
    """Citation framing for the contract."""

    enabled: bool = False
    cited_text_fields: tuple[str, ...] = ()


class NotationView(FrozenBaseModel):
    """URL substitution preservation guidance."""

    active: bool = False
    instruction: str = ""


class PromptRenderContext(FrozenBaseModel):
    """Typed template context for a PromptContract execution."""

    contract: ContractView
    inputs: dict[str, InputView] = Field(default_factory=dict)
    input_order: tuple[str, ...] = ()
    documents: tuple[DocumentRef, ...] = ()
    methodologies: tuple[MethodologyView, ...] = ()
    output: OutputView
    tools: tuple[ToolView, ...] = ()
    citations: CitationsView = Field(default_factory=CitationsView)
    notation: NotationView = Field(default_factory=NotationView)
    prose_field_name: str | None = None


def render_prompt(context: PromptRenderContext, template_source: str) -> str:
    """Render a full user prompt from framework sections and the paired Jinja body."""
    body = _render_body(context, template_source) if template_source else ""
    sections: list[str] = []
    if context.notation.active:
        sections.append(f"# Notation\n\n{context.notation.instruction}")
    if context.inputs:
        rendered_fields: list[str] = []
        for name in context.input_order:
            view = context.inputs[name]
            rendered_fields.append(f"## {view.title}\n\n{view.text}")
        sections.append("# Inputs\n\n" + "\n\n".join(rendered_fields))
    sections.append(f"# Purpose\n\n{context.contract.purpose}")
    sections.append(f"# Returns\n\n{context.contract.returns}")
    sections.append(f"# Success Criteria\n\n{context.contract.success_criteria}")
    sections.extend(_render_methodology(methodology) for methodology in context.methodologies)
    if context.prose_field_name is not None:
        sections.append(
            "# Response Format\n\nReturn your full response as markdown; do not wrap it in JSON or code fences."
        )
    if body:
        sections.append(f"# Instructions\n\n{body}")
    return "\n\n".join(sections)


def build_prompt_render_context(
    cls: type[Any],
    *,
    dynamic_fields: Mapping[str, Any],
    ordered_documents: tuple[tuple[str, Document], ...],
    notation_active: bool = False,
    prose_field_name: str | None = None,
) -> PromptRenderContext:
    """Build the documented template context from a contract class and partitioned fields."""
    docstring = cls.__doc__.strip() if cls.__doc__ and cls.__doc__.strip() else None
    contract_view = ContractView(
        class_name=cls.__name__,
        title=_pascal_to_title(cls.__name__.removesuffix("Contract")).strip() or cls.__name__,
        module=cls.__module__,
        purpose=cls.purpose,
        returns=cls.returns,
        success_criteria=cls.success_criteria,
        docstring=docstring,
    )
    inputs = {
        field_name: InputView(
            name=field_name,
            title=_field_label(field_name),
            kind=_input_kind(value),
            type_name=type(value).__name__,
            text=render_input_value(value),
        )
        for field_name, value in dynamic_fields.items()
    }
    output = _build_output_view(cls._output_type)
    cited_fields = tuple(field.name for field in output.fields if field.is_cited_text)
    return PromptRenderContext(
        contract=contract_view,
        inputs=inputs,
        input_order=tuple(inputs.keys()),
        documents=_build_document_refs(ordered_documents),
        methodologies=tuple(_build_methodology_view(methodology) for methodology in cls.methodologies),
        output=output,
        tools=_build_tool_views(cls),
        citations=CitationsView(enabled=bool(cited_fields), cited_text_fields=cited_fields),
        notation=NotationView(active=notation_active, instruction=_SUBSTITUTOR_INSTRUCTION if notation_active else ""),
        prose_field_name=prose_field_name,
    )


def _render_body(context: PromptRenderContext, template_source: str) -> str:
    env = _make_jinja_environment()
    return (
        env
        .from_string(template_source)
        .render(
            contract=context.contract,
            inputs=context.inputs,
            input_order=context.input_order,
            documents=context.documents,
            methodologies=context.methodologies,
            output=context.output,
            tools=context.tools,
            citations=context.citations,
            notation=context.notation,
        )
        .strip()
    )


def _build_document_refs(ordered_documents: tuple[tuple[str, Document], ...]) -> tuple[DocumentRef, ...]:
    refs: list[DocumentRef] = []
    counts: dict[str, int] = {}
    for field_name, document in ordered_documents:
        index = counts.get(field_name, 0)
        counts[field_name] = index + 1
        refs.append(
            DocumentRef(
                field_name=field_name,
                index=index,
                name=document.name,
                document_id=document.id,
                class_name=type(document).__name__,
                reference=f"{field_name}[{index}]",
            )
        )
    return tuple(refs)


def _build_output_view(output_type: type[BaseModel]) -> OutputView:
    fields: list[OutputFieldView] = []
    for field_name, field_info in output_type.model_fields.items():
        annotation = field_info.annotation
        fields.append(
            OutputFieldView(
                name=field_name,
                title=_field_label(field_name),
                type_name=getattr(annotation, "__name__", repr(annotation)),
                required=field_info.is_required(),
                is_cited_text=_is_cited_text_annotation(annotation),
            )
        )
    return OutputView(
        class_name=output_type.__name__,
        module=output_type.__module__,
        schema_text=json.dumps(output_type.model_json_schema(), indent=2, default=str),
        fields=tuple(fields),
        has_cited_text=any(field.is_cited_text for field in fields),
    )


def _build_tool_views(cls: type[Any]) -> tuple[ToolView, ...]:
    return tuple(
        ToolView(
            name=availability.tool.name,
            class_name=availability.tool.__name__,
            class_path=f"{availability.tool.__module__}:{availability.tool.__qualname__}",
            description=(
                availability.tool.__doc__.strip()
                if availability.tool.__doc__ and availability.tool.__doc__.strip()
                else None
            ),
            max_calls=availability.max_calls,
        )
        for availability in cls.tools
    )


def _render_methodology(view: MethodologyView) -> str:
    parts: list[str] = [f"# Reference: {view.title}"]
    if view.purpose:
        parts.append(view.purpose)
    if view.docstring:
        parts.append(view.docstring)
    if view.body:
        parts.append(view.body)
    parts.extend(f"## {field.title}\n\n{field.text}" for field in view.fields)
    return "\n\n".join(parts)


def _build_methodology_view(methodology: type[Methodology]) -> MethodologyView:
    title = _pascal_to_title(methodology.__name__.removesuffix("Methodology")).strip() or methodology.__name__
    purpose_value = vars(methodology).get("purpose", "")
    purpose = purpose_value.strip() if isinstance(purpose_value, str) and purpose_value.strip() else ""
    docstring = methodology.__doc__.strip() if methodology.__doc__ and methodology.__doc__.strip() else None
    fields = tuple(
        MethodologyFieldView(
            name=attr_name,
            title=_field_label(attr_name),
            type_name="str",
            text=value.strip(),
        )
        for attr_name, value in sorted(vars(methodology).items())
        if not attr_name.startswith("_") and attr_name != "purpose" and isinstance(value, str) and value.strip()
    )
    partial = MethodologyView(
        class_name=methodology.__name__,
        title=title,
        module=methodology.__module__,
        purpose=purpose,
        docstring=docstring,
        fields=fields,
    )
    body_source = getattr(methodology, "_body", "")
    if not body_source:
        return partial
    # Render the body against the body-less view, then rebuild the view once with the body.
    body = _render_methodology_body(partial, body_source)
    return MethodologyView(
        class_name=methodology.__name__,
        title=title,
        module=methodology.__module__,
        purpose=purpose,
        docstring=docstring,
        fields=fields,
        body=body,
    )


def _render_methodology_body(view: MethodologyView, template_source: str) -> str:
    env = _make_jinja_environment()
    return env.from_string(template_source).render(methodology=view).strip()


def _to_json_filter(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    return json.dumps(value, indent=2, default=str)


def _make_jinja_environment() -> jinja2.Environment:
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,  # noqa: S701  # templates render markdown, not HTML
        keep_trailing_newline=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["to_json"] = _to_json_filter
    return env
