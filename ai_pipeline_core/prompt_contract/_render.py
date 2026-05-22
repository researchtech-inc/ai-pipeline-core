"""PromptContract render abstraction.

Stage B PR 2: introduce a typed data view (``PromptRenderContext``) of a
prompt-contract execution and a renderer protocol that turns the view into
the user-facing prompt text. The default renderer (``DefaultPromptRenderer``)
produces output byte-identical to the previous in-place section assembler;
later PRs swap in a Jinja-backed renderer when a paired ``.md.j2`` template
exists.

The context shape mirrors the canonical end-to-end plan (codex round 80):
``contract``, ``inputs``, ``input_order``, ``documents``, ``methodologies``,
``output``, ``tools``, ``citations``, ``notation``. This PR populates the
fields the default renderer actually reads (``contract``, ``inputs``,
``methodologies``, ``notation``, ``body``); other sub-views land with
minimal accurate data so a Jinja-backed renderer can rely on the schema
in the next PR.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import jinja2
from pydantic import BaseModel, Field

from ai_pipeline_core._pydantic_base import FrozenBaseModel
from ai_pipeline_core.llm._conversation_runtime import _SUBSTITUTOR_INSTRUCTION

if TYPE_CHECKING:
    from ._contract import PromptContract
    from ._methodology import Methodology


__all__ = [
    "CitationsView",
    "ContractView",
    "DefaultPromptRenderer",
    "DocumentRef",
    "InputView",
    "JinjaPromptRenderer",
    "MethodologyFieldView",
    "MethodologyView",
    "NotationView",
    "OutputFieldView",
    "OutputView",
    "PromptRenderContext",
    "PromptRenderer",
    "ToolView",
    "build_prompt_render_context",
    "render_input_value",
    "select_renderer_for_contract",
]


logger = logging.getLogger(__name__)


_PASCAL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _pascal_to_title(name: str) -> str:
    """PascalCase -> Title Case for section headers."""
    return _PASCAL_BOUNDARY_RE.sub(" ", name)


def _field_label(field_name: str) -> str:
    """Field-name -> human label using the legacy assembler's rule."""
    return _pascal_to_title(field_name.replace("_", " ")).strip() or field_name


_PRIMITIVE_TYPES = (str, bool, int, float)


def render_input_value(value: Any) -> str:  # noqa: PLR0911 — type-dispatched renderer; each branch is a distinct supported field shape.
    """Render a contract instance-field value into prompt text.

    Type-dispatched: ``StrEnum`` renders via ``.value``; ``Literal`` field
    values reach here as plain primitives (str/int/bool) and render via
    ``str()``. ``BaseModel`` renders as indented JSON. List/tuple of
    ``BaseModel`` renders as a JSON array of indented JSON objects.
    List/tuple of primitives renders as a Markdown bullet list. Unknown
    types fall through to ``repr()`` with a warning so the contract author
    sees they are missing a type-specific branch.
    """
    # StrEnum check must come before ``str`` because StrEnum IS-A str.
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return value
    if isinstance(value, bool):  # bool IS-A int — check before int branch
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
            "PromptContract input field collection contains mixed/unsupported item types; falling back to repr(): %r",
            items,
        )
        return repr(items)
    if value is None:
        return "(none)"
    logger.warning(
        "PromptContract input field value has unsupported type %s; falling back to repr(): %r",
        type(value).__name__,
        value,
    )
    return repr(value)


def _input_kind(value: Any) -> Literal["scalar", "structured", "enum", "none"]:
    """Classify a dynamic-field value for the render context.

    The default renderer ignores ``kind``; templates use it to choose
    structured-vs-prose rendering.
    """
    if value is None:
        return "none"
    if isinstance(value, StrEnum):
        return "enum"
    if isinstance(value, BaseModel):
        return "structured"
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, BaseModel) for item in value):
        return "structured"
    return "scalar"


# ---------------------------------------------------------------------------
# Render context views
# ---------------------------------------------------------------------------


class ContractView(FrozenBaseModel):
    """Contract metadata available to renderers."""

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
    """Reference to one supplied document field item.

    PR 2 leaves ``PromptRenderContext.documents`` empty; the next PR
    populates document refs from the contract's document fields so Jinja
    templates can render their order and identifiers.
    """

    field_name: str
    index: int
    name: str
    document_id: str
    class_name: str
    reference: str


class MethodologyFieldView(FrozenBaseModel):
    """One public non-purpose ``ClassVar`` attached to a methodology."""

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
    """One field on the contract's structured output model."""

    name: str
    title: str
    type_name: str
    required: bool = True
    is_cited_text: bool = False


class OutputView(FrozenBaseModel):
    """The contract's structured output model.

    PR 2 surfaces only the class identity. The JSON schema string,
    per-field views, and cited-text detection land in the Jinja renderer
    PR. The schema string is exposed as ``schema_text`` rather than
    ``schema_json`` to avoid shadowing Pydantic's deprecated v1
    ``BaseModel.schema_json`` method (which triggers a noisy UserWarning
    at class definition time).
    """

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
    """Citation framing for the contract.

    PR 2 leaves both fields at their defaults; the Jinja renderer PR
    populates them from the output model's ``CitedText`` fields.
    """

    enabled: bool = False
    cited_text_fields: tuple[str, ...] = ()


class NotationView(FrozenBaseModel):
    """URL substitution preservation guidance.

    ``active`` is true on the second render pass when the substitutor
    actually applied; ``instruction`` carries the preservation string the
    model must follow.
    """

    active: bool = False
    instruction: str = ""


class PromptRenderContext(FrozenBaseModel):
    """Typed data view consumed by a ``PromptRenderer``.

    Shape mirrors the canonical end-to-end plan (codex round 80):
    ``contract``, ``inputs``, ``input_order``, ``documents``,
    ``methodologies``, ``output``, ``tools``, ``citations``, ``notation``.
    PR 2 populates the fields the default renderer actually reads;
    other sub-views land with minimal accurate data so a Jinja-backed
    renderer can rely on the canonical schema in the next PR.
    """

    contract: ContractView
    inputs: dict[str, InputView] = Field(default_factory=dict)
    input_order: tuple[str, ...] = ()
    documents: tuple[DocumentRef, ...] = ()
    methodologies: tuple[MethodologyView, ...] = ()
    output: OutputView
    tools: tuple[ToolView, ...] = ()
    citations: CitationsView = Field(default_factory=CitationsView)
    notation: NotationView = Field(default_factory=NotationView)
    body: str = ""


# ---------------------------------------------------------------------------
# Renderer protocol & default renderer
# ---------------------------------------------------------------------------


class PromptRenderer(ABC):
    """Turns a ``PromptRenderContext`` into the user-facing prompt text.

    The framework — not the contract author — chooses which renderer
    handles a given execution. PR 2 hard-codes ``DefaultPromptRenderer``;
    a later PR picks the Jinja renderer when a paired ``.md.j2`` template
    is present.

    Implemented as an ``ABC`` rather than ``Protocol`` so the codebase's
    semgrep ``protocol-in-dedicated-files`` rule does not need a new
    home for a one-method interface; the abstract-method contract is
    equivalent for our use.
    """

    @abstractmethod
    def render(self, context: PromptRenderContext) -> str: ...


def _to_json_filter(value: Any) -> str:
    """Render any value as a stable indented JSON string for templates.

    Pydantic ``BaseModel`` instances round-trip through ``model_dump_json``
    so nested validators and JSON aliases are honored. Everything else
    delegates to ``json.dumps`` with ``default=str`` so types that lack a
    native encoder (paths, enums, datetimes) render legibly rather than
    raising ``TypeError`` inside the renderer.
    """
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    return json.dumps(value, indent=2, default=str)


def _make_jinja_environment() -> jinja2.Environment:
    """Build the framework's standard Jinja environment.

    ``StrictUndefined`` turns unknown variables into hard render-time
    failures so template authors notice context drift immediately rather
    than shipping a prompt with silently-empty sections. The only custom
    filter registered is ``to_json``; the canonical schema does not
    permit additional filters in this PR.
    """
    # autoescape=False: rendered output is markdown for an LLM, not HTML; escaping would corrupt the prompt.
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,  # noqa: S701
        keep_trailing_newline=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["to_json"] = _to_json_filter
    return env


class JinjaPromptRenderer(PromptRenderer):
    """Jinja2-backed renderer for ``.md.j2`` template-driven contracts.

    Constructed with the loaded template source string. The renderer
    exposes the top-level fields of ``PromptRenderContext`` as Jinja
    variables (``contract``, ``inputs``, ``input_order``, ``documents``,
    ``methodologies``, ``output``, ``tools``, ``citations``,
    ``notation``, ``body``) using the canonical round-80 names so a
    template author writes once against the documented schema.
    """

    def __init__(self, template_source: str) -> None:
        self._source = template_source
        env = _make_jinja_environment()
        self._template = env.from_string(template_source)

    def render(self, context: PromptRenderContext) -> str:
        return self._template.render(
            contract=context.contract,
            inputs=context.inputs,
            input_order=context.input_order,
            documents=context.documents,
            methodologies=context.methodologies,
            output=context.output,
            tools=context.tools,
            citations=context.citations,
            notation=context.notation,
            body=context.body,
        )


def select_renderer_for_contract(cls: type[PromptContract[Any]]) -> PromptRenderer:
    """Pick the renderer for ``cls`` based on its discovered body format.

    ``_body_format == "jinja"`` selects ``JinjaPromptRenderer`` over the
    cached template source; everything else (``"static"``, ``"none"``)
    falls back to the deterministic ``DefaultPromptRenderer`` so contracts
    without a ``.md.j2`` template render through today's path unchanged.
    """
    body_format = getattr(cls, "_body_format", "static")
    body_source = getattr(cls, "_body", "")
    if body_format == "jinja" and body_source:
        return JinjaPromptRenderer(body_source)
    return DefaultPromptRenderer()


class DefaultPromptRenderer(PromptRenderer):
    """Deterministic Python renderer that mirrors the legacy section assembler.

    Output is byte-identical to the prior in-place ``_build_prompt_user_message``
    so introducing the renderer abstraction does not change what the model
    sees. The Jinja-backed renderer in the next PR takes over when a paired
    ``.md.j2`` template is present.
    """

    def render(self, context: PromptRenderContext) -> str:
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
        sections.extend(self._render_methodology(methodology) for methodology in context.methodologies)
        if context.body:
            sections.append(f"# Instructions\n\n{context.body}")
        return "\n\n".join(sections)

    @staticmethod
    def _render_methodology(view: MethodologyView) -> str:
        parts: list[str] = [f"# Reference: {view.title}"]
        if view.purpose:
            parts.append(view.purpose)
        if view.docstring:
            parts.append(view.docstring)
        parts.extend(f"## {field.title}\n\n{field.text}" for field in view.fields)
        if view.body:
            parts.append(view.body)
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_prompt_render_context(
    cls: type[PromptContract[Any]],
    *,
    dynamic_fields: Mapping[str, Any],
    notation_active: bool = False,
) -> PromptRenderContext:
    """Build a ``PromptRenderContext`` from a contract class and its partitioned fields.

    ``dynamic_fields`` is the set of non-document instance-field values;
    document fields are partitioned out by the caller and flow into the
    conversation as cacheable context messages. Document/output/tool
    sub-views are populated with minimal accurate data so the Jinja
    renderer in the next PR can rely on the canonical schema without
    changing today's prompt text.
    """
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

    inputs: dict[str, InputView] = {}
    for field_name, value in dynamic_fields.items():
        inputs[field_name] = InputView(
            name=field_name,
            title=_field_label(field_name),
            kind=_input_kind(value),
            type_name=type(value).__name__,
            text=render_input_value(value),
        )

    methodologies = tuple(_build_methodology_view(m) for m in cls.methodologies)

    output_type = cls._output_type
    output_view = OutputView(
        class_name=output_type.__name__,
        module=output_type.__module__,
    )

    tools = tuple(
        ToolView(
            name=availability.tool.name,
            class_name=availability.tool.__name__,
            class_path=f"{availability.tool.__module__}:{availability.tool.__qualname__}",
            description=(availability.tool.__doc__.strip() if availability.tool.__doc__ and availability.tool.__doc__.strip() else None),
            max_calls=availability.max_calls,
        )
        for availability in cls.tools
    )

    notation = NotationView(
        active=notation_active,
        instruction=_SUBSTITUTOR_INSTRUCTION if notation_active else "",
    )

    return PromptRenderContext(
        contract=contract_view,
        inputs=inputs,
        input_order=tuple(inputs.keys()),
        documents=(),
        methodologies=methodologies,
        output=output_view,
        tools=tools,
        notation=notation,
        body=cls._body,
    )


def _build_methodology_view(methodology: type[Methodology]) -> MethodologyView:
    """Assemble a ``MethodologyView`` from one methodology class.

    Mirrors the legacy ``_render_methodology_reference`` data-gathering
    rules: title strips the ``Methodology`` suffix and PascalCase-splits,
    purpose comes from the class's own ``vars()``, docstring is the
    class docstring, and public non-purpose string ClassVars become
    ``MethodologyFieldView`` items sorted by attr name. ``body`` is the
    paired markdown body file (empty for exempt classes); when the
    methodology's body file is a Jinja template the body is rendered
    here so downstream renderers see one finalized string regardless of
    discovery format.
    """
    title = _pascal_to_title(methodology.__name__.removesuffix("Methodology")).strip() or methodology.__name__
    purpose_value = vars(methodology).get("purpose", "")
    purpose = purpose_value.strip() if isinstance(purpose_value, str) and purpose_value.strip() else ""
    docstring = methodology.__doc__.strip() if methodology.__doc__ and methodology.__doc__.strip() else None
    fields: list[MethodologyFieldView] = []
    for attr_name in sorted(vars(methodology)):
        if attr_name.startswith("_") or attr_name == "purpose":
            continue
        value = vars(methodology)[attr_name]
        if isinstance(value, str) and value.strip():
            fields.append(
                MethodologyFieldView(
                    name=attr_name,
                    title=_field_label(attr_name),
                    type_name="str",
                    text=value.strip(),
                )
            )
    fields_tuple = tuple(fields)
    body_source = getattr(methodology, "_body", "")
    body_format = getattr(methodology, "_body_format", "static")
    if body_format == "jinja" and body_source:
        partial = MethodologyView(
            class_name=methodology.__name__,
            title=title,
            module=methodology.__module__,
            purpose=purpose,
            docstring=docstring,
            body="",
            fields=fields_tuple,
        )
        env = _make_jinja_environment()
        body = env.from_string(body_source).render(methodology=partial)
    else:
        body = body_source
    return MethodologyView(
        class_name=methodology.__name__,
        title=title,
        module=methodology.__module__,
        purpose=purpose,
        docstring=docstring,
        body=body,
        fields=fields_tuple,
    )
