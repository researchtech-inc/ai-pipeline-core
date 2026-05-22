"""PromptSpec base class with import-time validation."""

import annotationlib
import logging
import re
import typing
from collections.abc import Mapping
from textwrap import dedent
from typing import Any, ClassVar, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

from ai_pipeline_core._pydantic_generic import extract_generic_arg
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._file_rules import is_exempt, register_spec, register_stub, require_docstring

from .components import Guide, OutputRule, Role, Rule

logger = logging.getLogger(__name__)

OutputT = TypeVar("OutputT", default=str)

_XML_TAG_PATTERN = re.compile(r"</?[a-zA-Z]\w*[\s>/]")

_MAX_TASK_CHARS = 2000
_MAX_TASK_LINES = 40

# Pattern matching Python-style {identifier} placeholders in task text
_FIELD_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

_SPEC_KNOWN_ATTRS: frozenset[str] = frozenset({
    "_follows",
    "_output_type",
    "input_documents",
    "role",
    "task",
    "guides",
    "rules",
    "output_rules",
    "output_structure",
    "model_config",
})


def _check_no_duplicates(items: tuple[type, ...], *, attr: str, spec_name: str) -> None:
    """Reject duplicate entries in a spec tuple (rules, guides, etc.)."""
    seen: set[type] = set()
    for item in items:
        if item in seen:
            raise TypeError(f"PromptSpec '{spec_name}'.{attr} contains duplicate: {item.__name__}")
        seen.add(item)


def _validate_component_tuple(
    cls_dict: Mapping[str, Any],
    name: str,
    attr: str,
    expected_type: type,
    *,
    cross_check: type | None = None,
    cross_attr: str | None = None,
) -> tuple[type, ...]:
    """Validate a tuple of component class references (guides, rules, output_rules)."""
    items = cls_dict.get(attr, ())
    if not isinstance(items, tuple):
        raise TypeError(f"PromptSpec '{name}'.{attr} must be a tuple of {expected_type.__name__} subclasses")
    for item in cast(tuple[Any, ...], items):
        if not isinstance(item, type) or not issubclass(item, expected_type):
            if cross_check and isinstance(item, type) and issubclass(item, cross_check):
                raise TypeError(
                    f"PromptSpec '{name}'.{attr} contains {cross_check.__name__} '{item.__name__}'. "
                    f"Use {cross_attr}= for {'output formatting' if cross_check is OutputRule else 'behavioral'} constraints."
                )
            raise TypeError(f"PromptSpec '{name}'.{attr} contains non-{expected_type.__name__} class: {item!r}")
    validated = cast(tuple[type, ...], items)
    _check_no_duplicates(validated, attr=attr, spec_name=name)
    return validated


def _declared_annotations(cls: type) -> dict[str, Any]:
    """Return annotations declared directly on ``cls`` using Python 3.14 annotationlib."""
    annotate = annotationlib.get_annotate_from_class_namespace(cls.__dict__)
    if callable(annotate):
        annotations = cast(dict[str, Any], annotationlib.call_annotate_function(cast(Any, annotate), format=annotationlib.Format.FORWARDREF))
        return annotations

    return cast(dict[str, Any], annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF))


def _declared_field_names(cls: type) -> set[str]:
    """Return field names declared directly on ``cls`` during __init_subclass__."""
    annotated_fields = set(_declared_annotations(cls))
    field_info_names = {name for name, value in cls.__dict__.items() if not name.startswith("_") and isinstance(value, FieldInfo)}
    inherited_model_fields = {
        name for base in cls.__bases__ for name in (set(getattr(base, "__pydantic_fields__", {})) | set(getattr(base, "model_fields", {})))
    }
    own_model_fields = {
        name for name in (set(getattr(cls, "__pydantic_fields__", {})) | set(getattr(cls, "model_fields", {}))) if name not in inherited_model_fields
    }
    return annotated_fields | field_info_names | own_model_fields


def _check_unknown_attrs(cls: type, name: str) -> None:
    """Detect unknown class attributes that are likely typos.

    Runs during __init_subclass__ (before model_fields is populated), so it
    inspects only annotations declared directly on this class.
    """
    own_annotations = _declared_field_names(cls)
    for attr_name in cls.__dict__:
        if attr_name.startswith("_"):
            continue
        if attr_name in _SPEC_KNOWN_ATTRS:
            continue
        if attr_name in own_annotations:
            continue
        val = cls.__dict__[attr_name]
        if callable(val) or isinstance(val, (classmethod, staticmethod, property)):
            continue
        known = sorted(_SPEC_KNOWN_ATTRS - {"model_config", "_follows", "_output_type"})
        raise TypeError(f"PromptSpec '{name}' has unknown attribute '{attr_name}'. Known spec attributes: {', '.join(known)}")


def _check_field_descriptions(cls: type, name: str) -> None:
    """Validate that all Pydantic fields have Field(description=...).

    Uses definition-time annotations plus cls.__dict__ directly because
    model_fields is not yet populated during __init_subclass__.
    """
    for field_name in _declared_field_names(cls):
        if field_name in _SPEC_KNOWN_ATTRS:
            continue
        default = cls.__dict__.get(field_name)
        if isinstance(default, FieldInfo):
            if default.description is None:
                raise TypeError(f"PromptSpec '{name}' field '{field_name}' must use Field(description='...'). Bare Field() without description is not allowed.")
        else:
            raise TypeError(f"PromptSpec '{name}' field '{field_name}' must use Field(description='...'). Bare '{field_name}: ...' is not allowed.")


def _check_task_field_placeholders(cls: type, name: str) -> None:
    """Detect {field_name} placeholders in task text that reference declared Pydantic fields.

    The task ClassVar is rendered literally — {placeholders} are NOT substituted.
    Field values are rendered automatically in the Context section by the framework.
    """
    task = str(getattr(cls, "task", ""))
    field_names = {fn for fn in _declared_field_names(cls) if fn not in _SPEC_KNOWN_ATTRS}
    if not field_names:
        return

    refs_in_task = set(_FIELD_PLACEHOLDER_RE.findall(task))
    conflicts = sorted(refs_in_task & field_names)
    if not conflicts:
        return

    placeholders_str = ", ".join(f"{{{f}}}" for f in conflicts)
    raise TypeError(
        f"PromptSpec '{name}' task contains field placeholder references: {placeholders_str}.\n"
        f"\n"
        f"The 'task' ClassVar is rendered literally — {{field_name}} placeholders are NOT substituted with field values.\n"
        f"Field values are rendered automatically by the framework:\n"
        f"  - Regular fields (Field): inlined in the '# Context' section as '**description:**\\nvalue'\n"
        f"  - Multi-line fields (MultiLineField): sent as XML-tagged user messages (<field_name>value</field_name>)\n"
        f"    before the prompt, referenced in Context as '(provided in <field_name> tags in previous message)'\n"
        f"\n"
        f"Remove {{field_name}} references from task text. The task should describe WHAT to do,\n"
        f"not embed data placeholders. Reference data semantically instead.\n"
        f"\n"
        f"Example fix — instead of:\n"
        f'    task = "Analyze the {{topic}} using ..."\n'
        f"Write:\n"
        f'    task = "Analyze the topic identified in context using ..."'
    )


def _warn_large_task(name: str, task: str) -> None:
    """Warn when task text is excessively large — suggest using Guides for detailed instructions."""
    char_count = len(task)
    line_count = len(task.splitlines())
    if char_count <= _MAX_TASK_CHARS and line_count <= _MAX_TASK_LINES:
        return
    logger.warning(
        "PromptSpec '%s' task is too large (%d chars, %d lines). "
        "The task field should be a concise description of WHAT to do (up to %d chars / %d lines). "
        "Move detailed instructions, methodology, and step-by-step explanations into a Guide "
        "and reference it via guides=(MyGuide,).",
        name,
        char_count,
        line_count,
        _MAX_TASK_CHARS,
        _MAX_TASK_LINES,
    )


def _validate_prompt_spec(cls: type, name: str, follows: type[PromptSpec] | None) -> None:  # noqa: C901, PLR0912, PLR0915  # complexity acceptable; refactor would obscure structure
    """Validate a PromptSpec subclass at definition time."""
    # File-level structural rules — exemption check is early; register_spec()
    # still fires at the end so failed validation does not poison the registry.
    is_stub = getattr(cls, "_stub", False) is True  # set by __init_subclass__ keyword arg
    exempt = is_exempt(cls)

    # Block inheritance chains — must inherit directly from PromptSpec (or PromptSpec[T]).
    # Pydantic creates concrete classes for PromptSpec[T] with names like "PromptSpec[MyModel]".
    non_spec = [b.__name__ for b in cls.__bases__ if not (b is PromptSpec or (issubclass(b, PromptSpec) and "[" in b.__name__))]
    if non_spec or len(cls.__bases__) != 1:
        raise TypeError(f"PromptSpec '{name}' must inherit directly from PromptSpec, not from {', '.join(non_spec) or 'multiple bases'}")

    # Docstring required (with actionable error message for non-exempt application code)
    if not exempt:
        require_docstring(cls, kind="PromptSpec")
    elif cls.__doc__ is None or not cls.__doc__.strip():
        raise TypeError(f"PromptSpec '{name}' must define a non-empty docstring")

    # Detect 'follows' declared as a class body attribute (must use keyword syntax)
    if "follows" in cls.__dict__:
        value = cls.__dict__["follows"]
        hint = f"class {name}(PromptSpec, follows={value.__name__})" if isinstance(value, type) else f"class {name}(PromptSpec, follows=...)"
        raise TypeError(f"PromptSpec '{name}' declares 'follows' as a class attribute. Use the class keyword instead: {hint}")

    # Detect 'output_type' declared as a class body attribute (derived from generic parameter)
    if "output_type" in cls.__dict__:
        raise TypeError(f"PromptSpec '{name}' must not declare 'output_type' directly. Use the generic parameter instead: class {name}(PromptSpec[MyModel])")

    # Validate follows (runtime check — users may pass invalid types despite annotation)
    if follows is not None:
        # Cast to Any for runtime validation (callers may bypass type annotations)
        follows_raw: Any = follows
        if not isinstance(follows_raw, type) or not issubclass(follows_raw, PromptSpec):
            raise TypeError(f"PromptSpec '{name}': follows must be a PromptSpec subclass, got {follows_raw!r}")
        if follows is PromptSpec:
            raise TypeError(f"PromptSpec '{name}': follows must be a concrete PromptSpec subclass, not PromptSpec itself")
        if "[" in follows.__name__:
            raise TypeError(f"PromptSpec '{name}': follows must be a concrete PromptSpec subclass, not a parameterized generic")
    cls._follows = follows

    # Validate role (required for standalone specs, optional for follow-ups)
    if "role" not in cls.__dict__:
        if follows is None:
            raise TypeError(f"PromptSpec '{name}' must define 'role'")
        cls.role = None
    else:
        role = cls.__dict__["role"]
        if not isinstance(role, type) or not issubclass(role, Role):
            raise TypeError(f"PromptSpec '{name}'.role must be a Role subclass (class reference), got {role!r}")

    # Validate task
    if "task" not in cls.__dict__:
        raise TypeError(f"PromptSpec '{name}' must define 'task'")
    task = cls.__dict__["task"]
    if not isinstance(task, str):
        raise TypeError(f"PromptSpec '{name}'.task must be a string")
    cls.task = dedent(task).strip()
    if not cls.task:
        raise TypeError(f"PromptSpec '{name}'.task must not be empty")
    _warn_large_task(name, cls.task)

    # Validate input_documents (required for standalone specs, optional for follow-ups)
    if "input_documents" not in cls.__dict__:
        if follows is None:
            raise TypeError(f"PromptSpec '{name}' must define 'input_documents'")
        cls.input_documents = ()
    else:
        input_docs = cls.__dict__["input_documents"]
        if not isinstance(input_docs, tuple):
            raise TypeError(f"PromptSpec '{name}'.input_documents must be a tuple of Document subclasses")
        for doc_cls in cast(tuple[Any, ...], input_docs):
            if not isinstance(doc_cls, type) or not issubclass(doc_cls, Document):
                raise TypeError(f"PromptSpec '{name}'.input_documents contains non-Document class: {doc_cls!r}")
        _check_no_duplicates(cast(tuple[type[Document], ...], input_docs), attr="input_documents", spec_name=name)

    # Derive _output_type from generic parameter (PromptSpec[X] -> X)
    # Supports: str (default), BaseModel subclass, or list[BaseModel]
    output_type: Any = extract_generic_arg(cls, expected_origin=PromptSpec, default=str)
    if output_type is not str:
        # Check for list[BaseModel] pattern
        if typing.get_origin(output_type) is list:
            list_args = typing.get_args(output_type)
            if not list_args or not isinstance(list_args[0], type) or not issubclass(list_args[0], BaseModel):
                inner = repr(list_args[0]) if list_args else "?"
                raise TypeError(
                    f"PromptSpec '{name}' generic parameter list[T] requires T to be a BaseModel subclass, "
                    f"got list[{inner}]. "
                    f"Use PromptSpec[list[MyModel]] where MyModel is a BaseModel subclass."
                )
        elif not (isinstance(output_type, type) and issubclass(output_type, BaseModel)):
            raise TypeError(f"PromptSpec '{name}' generic parameter must be 'str', a BaseModel subclass, or list[BaseModel], got {output_type!r}")
    cls._output_type = output_type

    # Validate component tuples (optional, default empty)
    cls.guides = _validate_component_tuple(cls.__dict__, name, "guides", Guide)
    cls.rules = _validate_component_tuple(cls.__dict__, name, "rules", Rule, cross_check=OutputRule, cross_attr="output_rules")
    cls.output_rules = _validate_component_tuple(cls.__dict__, name, "output_rules", OutputRule, cross_check=Rule, cross_attr="rules")

    # Validate output_structure (optional)
    output_structure = cls.__dict__.get("output_structure")
    if output_structure is not None:
        if cls._output_type is not str:
            raise TypeError(f"PromptSpec '{name}'.output_structure is only allowed when output_type is str")
        if not isinstance(output_structure, str):
            raise TypeError(f"PromptSpec '{name}'.output_structure must be a string")
        cls.output_structure = dedent(output_structure).strip()
        if not cls.output_structure:
            raise TypeError(f"PromptSpec '{name}'.output_structure must not be empty")
        for line in cls.output_structure.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                raise TypeError(f"PromptSpec '{name}'.output_structure must not contain H1 headers ('# '). Use '## ' or deeper. Found: {line!r}")
    else:
        cls.output_structure = None

    # Validate OutputRules don't reference XML tags when output_structure is set
    if cls.output_structure is not None and cls.output_rules:
        for or_cls in cast(tuple[Any, ...], cls.output_rules):
            if _XML_TAG_PATTERN.search(str(or_cls.text)):
                raise TypeError(
                    f"PromptSpec '{name}' has output_structure with OutputRule "
                    f"'{or_cls.__name__}' that references XML tags. "
                    f"output_structure automatically adds <result> wrapping — remove XML instructions from the OutputRule."
                )

    # Validate Pydantic field descriptions (uses definition-time field metadata directly)
    _check_field_descriptions(cls, name)

    # Detect {field_name} placeholders in task text that reference declared fields
    _check_task_field_placeholders(cls, name)

    # Detect unknown class attributes (typos)
    _check_unknown_attrs(cls, name)

    # Register after all validation passes to avoid poisoning the registry on failure
    if not exempt:
        register_spec(cls, follows)
        if is_stub:
            register_stub(cls, kind="PromptSpec")


class PromptSpec[OutputT = str](BaseModel):
    r"""Base class for all prompt specifications.

    Generic parameter ``OutputT`` determines the output type:
    - ``PromptSpec[str]`` (or just ``PromptSpec``, default) for text output
    - ``PromptSpec[MyModel]`` for structured output (MyModel must be a BaseModel subclass)

    Must subclass PromptSpec directly — no inheritance chains allowed.
    Must define task on every PromptSpec subclass.
    Must not use ``{field}`` placeholders in task text — raises TypeError at definition time.
    Use dynamic Pydantic ``Field()`` parameters instead.
    Must define role and input_documents on standalone specs (not required when ``follows`` is set).
    Must use ``Field(description='...')`` for all dynamic Pydantic fields on PromptSpec subclasses.
    Must include all Guides that define terminology referenced in the task text — missing Guides
    cause the LLM to hallucinate definitions for framework-specific terms.
    Must ensure task vocabulary matches output model field names — when task text uses
    domain-specific terms but the output BaseModel has generic field names, add explicit
    mapping instructions in the task text.

    Required ClassVars: task. Also role and input_documents unless ``follows`` is set.
    Optional ClassVars: guides=(), rules=(), output_rules=(), output_structure=None.

    Class keyword parameter ``follows`` declares this spec as a follow-up to another spec.
    When ``follows`` is set, ``role`` and ``input_documents`` become optional (default to
    None and () respectively).

    ``input_documents`` declares Document types this spec expects in context. These are class
    references (types), not instances. Actual Document instances are passed via
    ``Conversation.send_spec(documents=[...])``.

    ``output_structure`` automatically enables ``<result>`` XML wrapping and auto-extraction
    in ``send_spec()``.
    Cannot combine ``output_structure`` with structured output (``PromptSpec[BaseModel]``) — output_structure is only for text specs.
    Never reference XML tags in OutputRules when output_structure is set — the framework adds ``<result>`` wrapping automatically.

    Never construct XML manually (f-string ``<document>`` tags) — the framework wraps Documents
    in XML automatically when they are added to the Conversation via ``with_context()`` or
    ``with_document()``. Use ``Document.create()`` to wrap dicts, lists, or BaseModel instances.

    Never use ``{field_name}`` placeholders in ``task`` text — the ``task`` ClassVar is rendered
    literally, not as a template. ``{field_name}`` appears as the literal string ``{field_name}``
    in the LLM prompt. Field values are rendered automatically by the framework: regular fields
    (``Field``) are inlined in the ``# Context`` section as ``**description:**\nvalue``, and
    multi-line fields (``MultiLineField``) are sent as XML-tagged user messages before the prompt.
    Instead of ``task = "Analyze the {topic} ..."``, write ``task = "Analyze the topic identified
    in context ..."``.

    Pydantic fields (dynamic input values):
        Any field declared with ``Field(description=...)`` becomes a dynamic input.
        Fields are for short, single-line parameter values (up to 500 characters) — e.g.,
        a topic name, finding type, or formatting instruction. Longer or multiline content
        (e.g., review feedback, website content, another model's output) must be passed as
        a Document via ``input_documents`` and ``send_spec(documents=[...])``.

    Four field types for PromptSpec dynamic inputs:
        - ``Field(description='...')`` — short single-line values, inlined in Context section
        - ``MultiLineField(description='...')`` — long/multiline strings, sent as XML-tagged
          user message before prompt: ``<field_name>value</field_name>``
        - ``StructuredField(description='...')`` — BaseModel values, rendered as JSON (indent=2)
          inside XML tags, sent as user message before prompt
        - ``ListField(description='...')`` — list values (``list[str]`` or ``list[BaseModel]``),
          each item individually tagged as ``<item_1>...<item_N>`` inside a wrapper tag, sent
          as user message before prompt. Context section shows item count.

    All four types require ``description``. StructuredField, ListField, and MultiLineField
    produce reference placeholders in the Context section instead of inlining their values.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _follows: ClassVar[type[PromptSpec] | None]
    _stub: ClassVar[bool] = False
    input_documents: ClassVar[tuple[type[Document], ...]]
    role: ClassVar[type[Role] | None]
    task: ClassVar[str]
    guides: ClassVar[tuple[type[Guide], ...]]
    rules: ClassVar[tuple[type[Rule], ...]]
    output_rules: ClassVar[tuple[type[OutputRule], ...]]
    _output_type: ClassVar[Any]  # type[str] | type[BaseModel] | list[type[BaseModel]]
    output_structure: ClassVar[str | None]

    def __init_subclass__(cls, *, follows: type[PromptSpec] | None = None, stub: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Pydantic creates concrete subclasses for parameterized generics (e.g. PromptSpec[str]).
        # These have names like "PromptSpec[str]" — skip validation for them.
        if "[" in cls.__name__:
            return

        cls._stub = stub
        _validate_prompt_spec(cls, cls.__name__, follows)


_MULTI_LINE_KEY = "multi_line"
_STRUCTURED_KEY = "structured_field"
_LIST_KEY = "list_field"


def MultiLineField(*, description: str, **kwargs: Any) -> Any:
    """Declare a multi-line field on a PromptSpec.

    Multi-line fields are combined into a single XML-tagged user message sent
    before the main prompt, not inlined in the Context section. Each field is
    wrapped as ``<field_name>value</field_name>``. Use for content that is long
    or multiline — e.g., review feedback, website content, another model's output.

    Must provide ``description``. Accepts all other ``Field()`` keyword arguments
    (``default``, ``default_factory``, etc.).
    """
    return Field(description=description, json_schema_extra={_MULTI_LINE_KEY: True}, **kwargs)


def StructuredField(*, description: str, **kwargs: Any) -> Any:
    """Declare a BaseModel field on a PromptSpec.

    Structured fields are rendered as JSON (indent=2) inside XML tags and sent
    as a user message before the main prompt. The Context section shows a
    reference placeholder instead of the value.

    The field annotation must be a BaseModel subclass.

    Must provide ``description``. Accepts all other ``Field()`` keyword arguments
    (``default``, ``default_factory``, etc.).
    """
    return Field(description=description, json_schema_extra={_STRUCTURED_KEY: True}, **kwargs)


def ListField(*, description: str, **kwargs: Any) -> Any:
    """Declare a list field on a PromptSpec.

    List fields are rendered as individually XML-tagged items inside a wrapper tag
    and sent as a user message before the main prompt. Each item is wrapped as
    ``<item_N>value</item_N>`` (1-indexed). BaseModel items are rendered as JSON
    (indent=2), strings are rendered as plain text.

    The field annotation must be ``list[X]`` where X is ``str`` or a BaseModel subclass.

    Must provide ``description``. Accepts all other ``Field()`` keyword arguments
    (``default``, ``default_factory``, etc.).
    """
    return Field(description=description, json_schema_extra={_LIST_KEY: True}, **kwargs)


def _is_multi_line_field(field_info: FieldInfo) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by render.py
    """Check whether a FieldInfo was created via MultiLineField."""
    extra = field_info.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get(_MULTI_LINE_KEY))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _is_structured_field(field_info: FieldInfo) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by render.py
    """Check whether a FieldInfo was created via StructuredField."""
    extra = field_info.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get(_STRUCTURED_KEY))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _is_list_field(field_info: FieldInfo) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by render.py
    """Check whether a FieldInfo was created via ListField."""
    extra = field_info.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get(_LIST_KEY))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _is_previous_message_field(field_info: FieldInfo) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by render.py
    """Check whether a field should be rendered as a previous message (any of the three field types)."""
    extra = field_info.json_schema_extra
    if not isinstance(extra, dict):
        return False
    return bool(extra.get(_MULTI_LINE_KEY) or extra.get(_STRUCTURED_KEY) or extra.get(_LIST_KEY))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


__all__ = ["ListField", "MultiLineField", "OutputT", "PromptSpec", "StructuredField"]
