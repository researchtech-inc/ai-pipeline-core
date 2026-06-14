"""PromptContract base class with import-time validation and live execution."""

import logging
import typing
from collections.abc import Callable
from dataclasses import asdict
from types import NoneType, UnionType
from typing import Annotated, Any, ClassVar, TypeVar, cast, get_args, get_origin

from pydantic import BaseModel, ConfigDict, ValidationError

from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.request import ValidationSpec
from ai_pipeline_core._llm_core.types import AIModelRef
from ai_pipeline_core._pydantic_base import FrozenBaseModel
from ai_pipeline_core._pydantic_generic import extract_generic_arg
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm._engine import EngineResult, InteractionRequest, ToolRuntime, execute_interaction
from ai_pipeline_core.llm._request_assembly import (
    assemble_api_messages,
    assert_llm_scope,
    cache_overrides,
    prepare_substitutor,
    restore_response,
    routing_overrides,
    tool_runtime,
)
from ai_pipeline_core.llm._request_messages import (
    AnyMessage,
    UserMessage,
    _core_messages_to_db_span_input,
    _response_format_path,
    _serialize_response_tool_calls,
)
from ai_pipeline_core.llm._tool_binding import ToolBinding
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_sinks
from ai_pipeline_core.pipeline._file_rules import is_exempt, register_contract, require_docstring
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.settings import settings

from .body_file import load_body_file
from .cited_text import CitedText, DocumentCitation
from .class_introspection import declared_annotations, is_classvar_annotation
from .continuation import Continues
from .methodology import Methodology
from .render import build_prompt_render_context, render_prompt
from .result import ContinuationState, PromptResult
from .tool_surface import ToolAvailability
from .validation import ValidationFailure

__all__ = ["OutputT", "PromptContract"]


logger = logging.getLogger(__name__)

OutputT = TypeVar("OutputT", bound=FrozenBaseModel)


_REQUIRED_CLASSVARS = ("purpose", "returns", "success_criteria")


_GENERIC_ARG_MISSING: Any = object()


def _unwrap_annotated(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        if args:
            return _unwrap_annotated(args[0])
    return annotation


def _prompt_result_output_type(annotation: Any) -> Any:
    annotation = _unwrap_annotated(annotation)
    if annotation is PromptResult:
        return _GENERIC_ARG_MISSING
    if get_origin(annotation) is PromptResult:
        args = get_args(annotation)
        return args[0] if args else _GENERIC_ARG_MISSING
    return None


def _is_document_annotation(annotation: Any) -> bool:
    annotation = _unwrap_annotated(annotation)
    if annotation is NoneType:
        return False
    origin = get_origin(annotation)
    if origin in {typing.Union, UnionType}:
        return any(_is_document_annotation(arg) for arg in get_args(annotation))
    if origin in {tuple, list}:
        args = get_args(annotation)
        return bool(args) and any(arg is not Ellipsis and _is_document_annotation(arg) for arg in args)
    return isinstance(annotation, type) and issubclass(annotation, Document)


def _instance_field_annotations(cls: type) -> dict[str, Any]:
    declared = getattr(cls, "__annotations__", {})
    try:
        resolved = typing.get_type_hints(cls, include_extras=True)
    except NameError, TypeError, AttributeError:
        resolved = declared
    return {
        field_name: resolved.get(field_name, annotation)
        for field_name, annotation in declared.items()
        if not is_classvar_annotation(annotation)
    }


def _single_required_str_field(output_type: type[FrozenBaseModel]) -> str | None:
    authored_fields = tuple(output_type.model_fields.items())
    if len(authored_fields) != 1:
        return None
    name, field_info = authored_fields[0]
    if _unwrap_annotated(field_info.annotation) is str and field_info.is_required():
        return name
    return None


def _check_no_duplicates(
    items: tuple[Any, ...],
    *,
    attr: str,
    name: str,
    key_fn: Callable[[Any], Any] | None = None,
) -> None:
    seen: set[Any] = set()
    for item in items:
        key = key_fn(item) if key_fn is not None else item
        label = getattr(item, "__name__", repr(item)) if key_fn is None else getattr(key, "__name__", repr(key))
        if key in seen:
            raise TypeError(f"PromptContract '{name}'.{attr} contains duplicate: {label}")
        seen.add(key)


def _validate_required_string(cls: type, name: str, attr: str, annotations: dict[str, Any]) -> None:
    if attr in annotations and not is_classvar_annotation(annotations[attr]):
        raise TypeError(
            f"PromptContract '{name}'.{attr} must be declared as 'ClassVar[str]' "
            f"(got '{attr}: {annotations[attr]!r} = ...'). Without the ClassVar annotation, "
            f"Pydantic treats it as an instance field and strips the class attribute, so "
            f"type(contract).{attr} disappears."
        )
    if attr not in cls.__dict__:
        raise TypeError(
            f"PromptContract '{name}' must define '{attr}' as a ClassVar[str] in its own "
            "class body. Inherited values do not satisfy the requirement."
        )
    value = cls.__dict__[attr]
    if not isinstance(value, str):
        raise TypeError(f"PromptContract '{name}'.{attr} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise TypeError(f"PromptContract '{name}'.{attr} must not be empty")


def _validate_methodologies(cls: type, name: str) -> tuple[type[Methodology], ...]:
    items = cls.__dict__.get("methodologies", ())
    if not isinstance(items, tuple):
        raise TypeError(f"PromptContract '{name}'.methodologies must be a tuple of Methodology subclasses")
    for item in cast(tuple[Any, ...], items):
        if not isinstance(item, type) or not issubclass(item, Methodology):
            raise TypeError(f"PromptContract '{name}'.methodologies contains non-Methodology class: {item!r}")
    validated = cast(tuple[type[Methodology], ...], items)
    _check_no_duplicates(validated, attr="methodologies", name=name)
    return validated


def _validate_tools(cls: type, name: str) -> tuple[ToolAvailability, ...]:
    items = cls.__dict__.get("tools", ())
    if not isinstance(items, tuple):
        raise TypeError(f"PromptContract '{name}'.tools must be a tuple of ToolAvailability values")
    for item in cast(tuple[Any, ...], items):
        if not isinstance(item, ToolAvailability):
            raise TypeError(
                f"PromptContract '{name}'.tools must contain "
                f"ToolAvailability(tool=..., max_calls=...) values, got {item!r}"
            )
    validated = cast(tuple[ToolAvailability, ...], items)

    def _tool_key(availability: ToolAvailability) -> type[Tool]:
        return availability.tool

    _check_no_duplicates(validated, attr="tools", name=name, key_fn=_tool_key)
    return validated


def _resolve_output_type(cls: type, name: str) -> type[FrozenBaseModel]:
    output_type = extract_generic_arg(cls, expected_origin=PromptContract, default=_GENERIC_ARG_MISSING)
    if output_type is _GENERIC_ARG_MISSING:
        raise TypeError(
            f"PromptContract '{name}' must declare a structured output type: "
            f"class {name}(PromptContract[MyModel]) "
            "where MyModel is a FrozenBaseModel subclass."
        )
    if not (isinstance(output_type, type) and issubclass(output_type, FrozenBaseModel)):
        raise TypeError(
            f"PromptContract '{name}' generic parameter must be a FrozenBaseModel subclass, got {output_type!r}"
        )
    return output_type


def _validate_continuation(cls: type, name: str, continues: Continues | None) -> None:
    predecessor_fields: list[tuple[str, Any]] = []
    for field_name, annotation in _instance_field_annotations(cls).items():
        output_type = _prompt_result_output_type(annotation)
        if output_type is not None:
            predecessor_fields.append((field_name, output_type))

    if continues is None:
        if predecessor_fields:
            field_names = ", ".join(field_name for field_name, _ in predecessor_fields)
            raise TypeError(
                f"PromptContract '{name}' declares PromptResult input field(s) without continues=: {field_names}. "
                "PromptResult fields are only valid on continuation contracts. "
                f"Declare class {name}(..., continues=Continues.once(OpenerContract)) or pass the prior "
                "response value as an ordinary structured input instead."
            )
        cls._continues = None
        cls._predecessor_field_name = None
        return

    if not isinstance(continues, Continues):  # type: ignore[unreachable]  # runtime defensive check despite static narrowing
        raise TypeError(  # type: ignore[unreachable]  # runtime defensive check despite static narrowing
            f"PromptContract '{name}' continues= must be Continues.once(OpenerContract) or "
            f"Continues.repeating(OpenerContract), got {continues!r}."
        )
    if not isinstance(continues.opener, type) or not issubclass(continues.opener, PromptContract):
        raise TypeError(
            f"PromptContract '{name}' continues= opener must be a PromptContract subclass. "
            "Use continues=Continues.once(OpenerContract) or Continues.repeating(OpenerContract)."
        )
    if len(predecessor_fields) != 1:
        raise TypeError(
            f"PromptContract '{name}' declares continues= but has {len(predecessor_fields)} PromptResult field(s). "
            "A continuation contract must declare exactly one predecessor field, for example "
            "prior: PromptResult[OpenerOutput] = Field(description='The turn this contract continues.')."
        )

    predecessor_field_name, predecessor_output_type = predecessor_fields[0]
    if predecessor_output_type is _GENERIC_ARG_MISSING:
        raise TypeError(
            f"PromptContract '{name}' field '{predecessor_field_name}' must parameterize PromptResult with the "
            "predecessor output model, for example prior: PromptResult[OpenerOutput]."
        )

    legal_contracts = (continues.opener, cls) if continues.repeatable else (continues.opener,)
    legal_outputs = tuple(contract._output_type for contract in legal_contracts)
    if predecessor_output_type not in legal_outputs:
        legal_names = ", ".join(output.__name__ for output in legal_outputs)
        raise TypeError(
            f"PromptContract '{name}' field '{predecessor_field_name}' is PromptResult[{predecessor_output_type}], "
            f"but this continuation may only continue outputs from: {legal_names}. "
            "Change the field annotation to the opener output type, or change continues= to the intended opener."
        )

    cls._continues = continues
    cls._predecessor_field_name = predecessor_field_name


def _validate_prompt_contract(cls: type, name: str, continues: Continues | None) -> None:
    exempt = is_exempt(cls)

    if not name.endswith("Contract"):
        raise TypeError(f"PromptContract subclass '{name}' name must end with 'Contract'.")

    non_contract = [
        b.__name__
        for b in cls.__bases__
        if not (b is PromptContract or (issubclass(b, PromptContract) and "[" in b.__name__))
    ]
    if non_contract or len(cls.__bases__) != 1:
        raise TypeError(
            f"PromptContract '{name}' must inherit directly from PromptContract "
            f"(or PromptContract[T]), not from "
            f"{', '.join(non_contract) or 'multiple bases'}"
        )

    if not exempt:
        require_docstring(cls, kind="PromptContract")
    elif cls.__doc__ is None or not cls.__doc__.strip():
        raise TypeError(f"PromptContract '{name}' must define a non-empty docstring")

    annotations = declared_annotations(cls)

    for attr_name in _REQUIRED_CLASSVARS:
        _validate_required_string(cls, name, attr_name, annotations)

    cls.methodologies = _validate_methodologies(cls, name)
    cls.tools = _validate_tools(cls, name)

    cls._output_type = _resolve_output_type(cls, name)
    body_file = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=exempt)
    cls._body = body_file.source
    cls._prose_field_name = _single_required_str_field(cls._output_type)
    _validate_continuation(cls, name, continues)

    if not exempt:
        register_contract(cls)


class PromptContract[OutputT: FrozenBaseModel](BaseModel):
    """Base class for typed prompt contracts.

    A ``PromptContract`` represents one structured-output prompt execution:
    a purpose, an expected return, success criteria, optional methodologies,
    and optional tools. Subclasses are Pydantic models — instance fields
    carry the dynamic inputs (documents, parameters, structured payloads)
    for that execution.

    Required class body declarations (must appear in the subclass's own
    ``__dict__``; inherited values do not count):

    - ``purpose: ClassVar[str]`` — what this contract accomplishes
    - ``returns: ClassVar[str]`` — description of the structured output
    - ``success_criteria: ClassVar[str]`` — how success is judged

    Optional:

    - ``methodologies: ClassVar[tuple[type[Methodology], ...]]`` (default ``()``)
    - ``tools: ClassVar[tuple[ToolAvailability, ...]]`` (default ``()``)
    - ``continues=Continues.once(...)`` or ``Continues.repeating(...)`` for
      task-local continuation.

    Subclass names must end with ``Contract``. The generic parameter
    ``OutputT`` must be a ``BaseModel`` subclass.

    Rule lines enforced at class definition:
    - Each non-exempt subclass has exactly one paired ``<stem>.j2`` body file.
    - Document fields are sent to the model in declaration order; document tuples keep caller order.
    - ``PromptResult`` input fields are only allowed on contracts that declare ``continues=``.
    - A continuation contract has exactly one legal predecessor ``PromptResult`` field.
    - Additional continuation document fields are appended after prior turns and do not affect the prompt cache key.
    - An output model with one required plain ``str`` field uses markdown transport and is wrapped into the model.

    Override ``validate(response)`` to add semantic checks; non-empty
    failure tuples drive the engine's repair loop. The repair budget is
    framework-owned (``settings.prompt_contract_max_repair``); contracts
    cannot tune it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purpose: ClassVar[str]
    returns: ClassVar[str]
    success_criteria: ClassVar[str]
    methodologies: ClassVar[tuple[type[Methodology], ...]] = ()
    tools: ClassVar[tuple[ToolAvailability, ...]] = ()
    _output_type: ClassVar[type[FrozenBaseModel]]
    _body: ClassVar[str] = ""
    _continues: ClassVar[Continues | None] = None
    _predecessor_field_name: ClassVar[str | None] = None
    _prose_field_name: ClassVar[str | None] = None
    _pending_continues: ClassVar[Continues | None] = None

    def __init_subclass__(cls, *, continues: Continues | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._pending_continues = continues

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:  # noqa: PLW3201  # pydantic hook fires after model_fields populate; __init_subclass__ is too early
        super().__pydantic_init_subclass__(**kwargs)
        if "[" in cls.__name__:
            return
        _validate_prompt_contract(cls, cls.__name__, cls._pending_continues)

    # ``BaseModel.validate`` is a deprecated v1 classmethod alias; we shadow it
    # intentionally with an instance method so contract subclasses can read
    # ``self`` while running semantic checks on the parsed response.
    def validate(self, response: OutputT) -> tuple[ValidationFailure, ...]:  # type: ignore[override]  # noqa: PLR6301  # method exposed as ABC override; instance self required
        """Override to add semantic validation. Default: always passes."""
        _ = response
        return ()

    async def execute(  # noqa: PLR0914, PLR0915 — engine dispatch unavoidably touches many locals; further extraction would obscure the single-shot flow.
        self,
        model: AIModelRef,
        *,
        tool_bindings: tuple[ToolBinding, ...] = (),
    ) -> PromptResult[OutputT]:
        """Execute this contract against ``model`` and return a parsed result."""
        if not isinstance(model, AIModelRef):  # type: ignore[unreachable]  # runtime defensive check despite static type
            raise TypeError(f"PromptContract.execute(model=...) requires an AIModelRef; got {type(model).__name__}.")  # type: ignore[unreachable]  # runtime defensive check despite static narrowing

        # Refuse to dispatch the LLM when called directly from a flow body without a task.
        assert_llm_scope(source="PromptContract.execute()")

        cls = type(self)
        runtime = _build_tool_runtime(cls, tool_bindings)
        ordered_documents, dynamic_fields = _partition_instance_fields(self)
        predecessor = _predecessor_continuation(self)
        if predecessor is not None:
            _validate_continuation_model_compatibility(cls, predecessor, model)
        context_tuple = (
            tuple(document for _, document in ordered_documents) if predecessor is None else predecessor.context
        )
        prior_turns = () if predecessor is None else predecessor.turns
        followup_documents = () if predecessor is None else tuple(document for _, document in ordered_documents)
        prose_field_name = cls._prose_field_name

        first_pass_context = build_prompt_render_context(
            cls,
            dynamic_fields=dynamic_fields,
            ordered_documents=ordered_documents,
            notation_active=False,
            prose_field_name=prose_field_name,
        )
        first_pass_text = render_prompt(first_pass_context, cls._body)
        first_pass_messages = prior_turns + followup_documents + (UserMessage(first_pass_text),)
        substitutor = prepare_substitutor(
            context=context_tuple,
            messages=first_pass_messages,
            enabled=True,
            model=model,
        )
        substitutor_active = substitutor is not None and substitutor.pattern_count > 0
        if substitutor_active:
            final_context = build_prompt_render_context(
                cls,
                dynamic_fields=dynamic_fields,
                ordered_documents=ordered_documents,
                notation_active=True,
                prose_field_name=prose_field_name,
            )
            prompt_text = render_prompt(final_context, cls._body)
        else:
            prompt_text = first_pass_text
        messages_tuple = prior_turns + followup_documents + (UserMessage(prompt_text),)

        core_messages, context_count = await assemble_api_messages(
            system_block=None,
            context=context_tuple,
            messages=messages_tuple,
            model=model,
            substitutor=substitutor,
        )

        validation_spec = ValidationSpec(
            validate=self._coerce_and_validate,
            max_attempts=settings.prompt_contract_max_repair,
        )
        purpose_label = type(self).__name__
        execution_ctx = get_execution_context()
        preferred_deployment_id = None  # PromptContract is single-shot at the public boundary.
        cache_override = cache_overrides(options=None)
        request = InteractionRequest(
            messages=tuple(core_messages),
            model=model,
            context_count=context_count,
            response_format=None if prose_field_name is not None else cls._output_type,
            tools=runtime,
            substitutor=substitutor,
            purpose=purpose_label,
            cache_overrides=cache_override,
            routing_overrides=routing_overrides(purpose_label, preferred_deployment_id=preferred_deployment_id),
            validation=validation_spec,
        )

        receiver_payload = {
            "mode": "promptcontract_receiver",
            "value": {
                "class_path": f"{cls.__module__}:{cls.__qualname__}",
                "instance_fields": {field_name: getattr(self, field_name) for field_name in cls.model_fields},
            },
        }
        # Persist tool_bindings into the recorded input so replay can rebuild them.
        span_input: dict[str, Any] = {"model": model, "tool_bindings": tool_bindings}
        target = f"decoded_promptcontract:{cls.__module__}:{cls.__qualname__}.execute"

        async with track_span(
            SpanKind.PROMPT_EXECUTION,
            purpose_label,
            target,
            sinks=get_sinks(),
            encode_receiver=receiver_payload,
            encode_input=span_input,
            db=execution_ctx.database if execution_ctx is not None else None,
        ) as span_ctx:
            result: EngineResult = await execute_interaction(request)
            response = result.response
            if substitutor is not None and substitutor.pattern_count > 0:
                response = restore_response(response, substitutor, request.response_format)

            parsed = _build_output_model(self, response)
            if not isinstance(parsed, cls._output_type):
                raise TypeError(
                    f"PromptContract '{purpose_label}' expected parsed response of "
                    f"{cls._output_type.__name__}, got {type(parsed).__name__}."
                )

            combined_citations = _build_prompt_result_citations(parsed, response.citations)
            restored_turns = _restored_accumulated_messages(result, response)
            continuation = ContinuationState(
                context=context_tuple,
                turns=messages_tuple + restored_turns,
                source_span_id=str(span_ctx.span_id),
                model_cache_fingerprint=_model_cache_fingerprint(model),
            )
            span_ctx.set_meta(
                **_prompt_contract_meta(
                    cls=cls,
                    response=response,
                    request=request,
                    llm_round_count=result.llm_round_count,
                    repair_attempt_count=result.repair_attempt_count,
                    tool_bindings=tool_bindings,
                    citations=combined_citations,
                    ordered_documents=ordered_documents,
                    followup_document_count=len(followup_documents),
                    continuation=continuation,
                    predecessor=predecessor,
                    prose_field_name=prose_field_name,
                )
            )
            span_ctx.set_metrics(
                first_token_ms=int((response.transport.timing.first_token_s or 0) * 1000),
            )
            span_ctx.set_output_preview({"model": response.model, "content": response.content[:1000]})
            # cast(): the runtime ``isinstance(parsed, cls._output_type)`` above
            # narrows ``parsed`` semantically, but the type-checker cannot follow
            # the dynamic ClassVar to bind ``OutputT``.
            prompt_result: PromptResult[OutputT] = PromptResult(
                response=cast(OutputT, parsed),
                citations=combined_citations,
                _continuation=continuation,
            )
            span_ctx._set_output_value(prompt_result)
            return prompt_result

    def _coerce_and_validate(self, parsed: Any) -> tuple[ValidationFailure, ...]:
        """Coerce prose transport into the output model before running contract validation."""
        output = _build_output_model_from_parsed(self, parsed)
        if isinstance(output, tuple):
            return output
        return self.validate(cast(OutputT, output))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tool_runtime(
    cls: type[PromptContract[Any]],
    tool_bindings: tuple[ToolBinding, ...],
) -> ToolRuntime | None:
    """Bridge declared ToolAvailability + ToolBinding(args) into a ToolRuntime."""
    declared = cls.tools
    if not declared and not tool_bindings:
        return None
    if not declared and tool_bindings:
        raise TypeError(
            f"PromptContract '{cls.__name__}' has no tools declared but "
            f"{len(tool_bindings)} tool_bindings were provided. "
            "Declare tools via 'tools: ClassVar[tuple[ToolAvailability, ...]] = (...)' "
            "or remove the bindings."
        )
    bindings_by_tool: dict[type[Tool], ToolBinding] = {}
    for binding in tool_bindings:
        if binding.tool in bindings_by_tool:
            raise TypeError(
                f"PromptContract '{cls.__name__}' received duplicate ToolBinding for {binding.tool.__name__}."
            )
        bindings_by_tool[binding.tool] = binding

    instances: list[Tool] = []
    for availability in declared:
        binding = bindings_by_tool.get(availability.tool)
        if binding is None:
            raise TypeError(
                f"PromptContract '{cls.__name__}' declares tool "
                f"{availability.tool.__name__} but no matching ToolBinding was provided. "
                "Pass tool_bindings=(ToolBinding(<Tool>, args={...}),) to execute()."
            )
        try:
            instance = availability.tool(**dict(binding.args))
        except TypeError as exc:
            raise TypeError(
                f"PromptContract '{cls.__name__}' could not construct "
                f"{availability.tool.__name__}(**{dict(binding.args)!r}): {exc}"
            ) from exc
        instances.append(instance)

    extra_bound = [
        binding for binding in tool_bindings if binding.tool not in {availability.tool for availability in declared}
    ]
    if extra_bound:
        names = ", ".join(binding.tool.__name__ for binding in extra_bound)
        raise TypeError(
            f"PromptContract '{cls.__name__}' received tool_bindings for undeclared "
            f"tools: {names}. Add them to the contract's 'tools' ClassVar first."
        )

    max_rounds = sum(availability.max_calls for availability in declared)
    max_calls_by_name = {availability.tool.name: availability.max_calls for availability in declared}
    return tool_runtime(instances, tool_choice=None, max_tool_rounds=max_rounds, max_calls_by_name=max_calls_by_name)


def _partition_instance_fields(
    instance: PromptContract[Any],
) -> tuple[tuple[tuple[str, Document], ...], dict[str, Any]]:
    """Split instance fields into ordered documents and non-document render inputs."""
    docs: list[tuple[str, Document]] = []
    dynamic: dict[str, Any] = {}
    cls = type(instance)
    for field_name, field_info in cls.model_fields.items():
        if field_name == cls._predecessor_field_name:
            continue
        value = getattr(instance, field_name)
        if _is_document_annotation(field_info.annotation):
            if value is None:
                continue
            if isinstance(value, Document):
                docs.append((field_name, value))
                continue
            if isinstance(value, (list, tuple)) and all(isinstance(item, Document) for item in value):
                docs.extend((field_name, item) for item in value)
                continue
            raise TypeError(
                f"PromptContract '{cls.__name__}' field '{field_name}' is annotated as a Document input but "
                f"has value of type {type(value).__name__}. Pass a Document instance, a tuple of Document "
                "instances, or None for optional document fields."
            )
        dynamic[field_name] = value
    return tuple(docs), dynamic


def _predecessor_continuation(instance: PromptContract[Any]) -> ContinuationState | None:
    field_name = type(instance)._predecessor_field_name
    if field_name is None:
        return None
    predecessor = getattr(instance, field_name)
    if not isinstance(predecessor, PromptResult):
        raise TypeError(
            f"PromptContract '{type(instance).__name__}' predecessor field '{field_name}' must contain a "
            "PromptResult returned by the contract it continues."
        )
    if predecessor._continuation is None:
        raise TypeError(
            f"PromptContract '{type(instance).__name__}' predecessor field '{field_name}' has no continuation "
            "state. Pass the PromptResult returned directly by PromptContract.execute() inside the same task."
        )
    return predecessor._continuation


def _model_cache_fingerprint(model: AIModelRef) -> tuple[str, bool, bool]:
    return (
        model.vision_preset.value,
        model.preserve_input_urls,
        model.supports_url_substitution,
    )


def _validate_continuation_model_compatibility(
    cls: type[PromptContract[Any]],
    predecessor: ContinuationState,
    model: AIModelRef,
) -> None:
    fingerprint = _model_cache_fingerprint(model)
    if fingerprint == predecessor.model_cache_fingerprint:
        return
    raise TypeError(
        f"PromptContract '{cls.__name__}' continues a previous PromptContract result with a different "
        "model context-rendering configuration. Continuations must use the same AIModelRef rendering behavior "
        "as the opener so the initial document prefix produces the same prompt cache key. "
        "Use the same AIModelRef for the opener and continuation, or start a fresh non-continuation contract."
    )


def _build_output_model(instance: PromptContract[Any], response: ModelResponse[Any]) -> FrozenBaseModel:
    output = _build_output_model_from_parsed(instance, response.parsed)
    if isinstance(output, tuple):
        raise TypeError(output[0].message if output else "PromptContract response could not be coerced.")
    return output


def _build_output_model_from_parsed(
    instance: PromptContract[Any],
    parsed: Any,
) -> FrozenBaseModel | tuple[ValidationFailure, ...]:
    cls = type(instance)
    prose_field_name = cls._prose_field_name
    if prose_field_name is None:
        if isinstance(parsed, cls._output_type):
            return parsed
        return (
            ValidationFailure(
                message=(
                    f"Response parsed as {type(parsed).__name__}, but {cls.__name__} requires "
                    f"{cls._output_type.__name__}. Re-emit the response in the declared structure."
                )
            ),
        )
    if not isinstance(parsed, str):
        return (
            ValidationFailure(
                field=prose_field_name,
                message=(
                    f"Response parsed as {type(parsed).__name__}, but prose-mode field '{prose_field_name}' "
                    "requires plain markdown text."
                ),
            ),
        )
    try:
        return cls._output_type(**{prose_field_name: parsed})
    except (TypeError, ValueError, ValidationError) as exc:
        return (
            ValidationFailure(
                field=prose_field_name,
                message=f"Response text could not be assigned to '{prose_field_name}': {exc}",
            ),
        )


def _restored_accumulated_messages(result: EngineResult, response: ModelResponse[Any]) -> tuple[AnyMessage, ...]:
    accumulated = list(result.accumulated_messages)
    for index in range(len(accumulated) - 1, -1, -1):
        if isinstance(accumulated[index], ModelResponse):
            accumulated[index] = response
            break
    return tuple(accumulated)


def _engine_citation_to_document_citation(citation: Citation) -> DocumentCitation:
    """Adapt one engine ``Citation`` (URL annotation) into ``DocumentCitation``.

    Stage A: URL citations from search-enabled models carry ``url`` and
    ``title``; ``document_id`` is left ``None`` and ``field`` is ``None``
    because the engine citation is execution-level, not output-field-scoped.
    Document-grounded citations surfaced through ``CitedText`` fields are
    collected by ``_collect_response_citations`` and carry a populated
    ``field`` path.

    Integer offsets (including ``0``) are preserved exactly. The response
    builder legitimately emits ``start_index=0, end_index=0`` for grounding
    and flat-URL citations, so the prior ``or None`` shortcut was wrong —
    only an actually-``None`` source value maps to ``None``.
    """
    return DocumentCitation(
        url=citation.url or None,
        title=citation.title or None,
        start_index=citation.start_index if citation.start_index is not None else None,
        end_index=citation.end_index if citation.end_index is not None else None,
    )


def _collect_response_citations(response: Any, *, _path: str = "") -> list[DocumentCitation]:
    """Recursively walk ``response`` and collect ``CitedText`` citations.

    Each collected ``DocumentCitation`` carries the dotted field path on
    which it was found (e.g. ``"body"``, ``"findings[0].summary"``). The
    walker descends into ``BaseModel`` fields, ``tuple``/``list``, and
    ``dict`` values. Plain scalars are ignored. ``field`` is stamped at
    the ``CitedText`` location, not at every nested element below it.
    """
    collected: list[DocumentCitation] = []

    if isinstance(response, CitedText):
        for citation in response.citations:
            if citation.field == _path:
                collected.append(citation)
            else:
                collected.append(
                    DocumentCitation(
                        document_id=citation.document_id,
                        url=citation.url,
                        title=citation.title,
                        start_index=citation.start_index,
                        end_index=citation.end_index,
                        field=_path or None,
                    )
                )
        return collected

    if isinstance(response, BaseModel):
        for field_name in type(response).model_fields:
            value = getattr(response, field_name, None)
            child_path = f"{_path}.{field_name}" if _path else field_name
            collected.extend(_collect_response_citations(value, _path=child_path))
        return collected

    if isinstance(response, (list, tuple)):
        for index, item in enumerate(response):
            child_path = f"{_path}[{index}]"
            collected.extend(_collect_response_citations(item, _path=child_path))
        return collected

    if isinstance(response, dict):
        for key, value in response.items():
            child_path = f"{_path}.{key}" if _path else str(key)
            collected.extend(_collect_response_citations(value, _path=child_path))
        return collected

    return collected


def _dedupe_document_citations(citations: list[DocumentCitation]) -> tuple[DocumentCitation, ...]:
    """Deduplicate citations by (field, document_id, url, start_index, end_index)."""
    seen: set[tuple[str | None, str | None, str | None, int | None, int | None]] = set()
    unique: list[DocumentCitation] = []
    for citation in citations:
        key = (citation.field, citation.document_id, citation.url, citation.start_index, citation.end_index)
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return tuple(unique)


def _build_prompt_result_citations(
    response: Any, engine_citations: tuple[Citation, ...]
) -> tuple[DocumentCitation, ...]:
    """Merge engine URL citations with parsed-response ``CitedText`` citations.

    Engine citations land first (with ``field=None``); response citations
    follow with ``field`` populated by the walker. The combined set is
    deduplicated on its full identity tuple so an engine URL citation and
    a structurally identical response citation collapse only when both
    carry the same ``field`` value — and they do not, because engine
    citations always have ``field=None``. This is the same combined set
    used in the prompt-execution span meta.
    """
    merged: list[DocumentCitation] = [_engine_citation_to_document_citation(citation) for citation in engine_citations]
    merged.extend(_collect_response_citations(response))
    return _dedupe_document_citations(merged)


def _prompt_contract_meta(
    *,
    cls: type[PromptContract[Any]],
    response: ModelResponse[Any],
    request: InteractionRequest,
    llm_round_count: int,
    repair_attempt_count: int,
    tool_bindings: tuple[ToolBinding, ...],
    citations: tuple[DocumentCitation, ...],
    ordered_documents: tuple[tuple[str, Document], ...],
    followup_document_count: int,
    continuation: ContinuationState,
    predecessor: ContinuationState | None,
    prose_field_name: str | None,
) -> dict[str, Any]:
    """Build PROMPT_EXECUTION span meta.

    Mirrors the shape of ``Conversation._conversation_meta`` for the keys the two
    surfaces share (model, response_*, aipl_summary, transport blocks) and adds
    the contract-specific keys called out in the Stage 2 plan: ``purpose_text``,
    ``returns_text``, ``success_criteria_text``, ``methodologies``, ``tool_bindings``,
    and ``repair_attempt_count``.
    """
    runtime = request.tools if isinstance(request.tools, ToolRuntime) else None
    tool_choice = runtime.choice if runtime is not None else None
    aipl_summary = {
        "call_id": response.transport.aipl.call_id,
        "deployment_id": response.transport.aipl.deployment_id,
        "provider": response.transport.aipl.provider,
        "group_status": response.transport.aipl.group_status,
        "response_cache_hit": response.transport.aipl.response_cache_hit,
        "round_count": llm_round_count,
        "repair_attempt_count": repair_attempt_count,
        "tool_round_count": 1 if response.has_tool_calls else 0,
        "tried_deployments": tuple(response.transport.aipl.tried_deployments),
        "failed_deployments": tuple(response.transport.aipl.failed_deployments),
    }
    return {
        # Shared with Conversation meta:
        "purpose": request.purpose,
        "model": response.model,
        "response_content": response.content,
        "reasoning_content": response.reasoning_content,
        "response_id": response.response_id,
        "response_format_path": _response_format_path(request.response_format),
        "citations": tuple(citation.model_dump(mode="json") for citation in citations),
        "tool_schemas": list(runtime.schemas) if runtime is not None else [],
        "tool_choice": tool_choice,
        "max_tool_rounds": getattr(runtime, "max_rounds", 0),
        "aipl_summary": aipl_summary,
        "aipl": asdict(response.transport.aipl),
        "litellm": asdict(response.transport.litellm),
        "model_chain": asdict(response.transport.model_chain),
        "prompt_cache_key": response.transport.prompt_cache_key,
        "context_document_ids": tuple(document.id for _, document in ordered_documents),
        "cache_prefix_document_ids": tuple(document.id for document in continuation.context),
        "followup_document_count": followup_document_count,
        "continuation_used": predecessor is not None,
        "continuation_predecessor_span_id": predecessor.source_span_id if predecessor is not None else None,
        "continuation_span_id": continuation.source_span_id,
        "continues_opener": (
            f"{cls._continues.opener.__module__}:{cls._continues.opener.__qualname__}"
            if cls._continues is not None
            else None
        ),
        "continues_repeatable": cls._continues.repeatable if cls._continues is not None else False,
        "prose_field_name": prose_field_name,
        "response_transport_mode": "markdown" if prose_field_name is not None else "structured",
        "raw_response_headers": dict(response.transport.raw_response_headers),
        # LLM_ROUND-shaped fields included at the contract level for parity:
        "finish_reason": response.finish_reason,
        "response_tool_calls": _serialize_response_tool_calls(response.tool_calls),
        "tool_call_count": len(response.tool_calls),
        "request_messages": _core_messages_to_db_span_input(list(request.messages)),
        # Contract-specific (new per Stage 2 plan):
        "purpose_text": cls.purpose,
        "returns_text": cls.returns,
        "success_criteria_text": cls.success_criteria,
        "methodologies": [f"{m.__module__}:{m.__qualname__}" for m in cls.methodologies],
        "tools": [
            {"name": tb.tool.name, "class_path": f"{tb.tool.__module__}:{tb.tool.__qualname__}"} for tb in tool_bindings
        ],
        "tool_bindings": [
            {"tool_class_path": f"{tb.tool.__module__}:{tb.tool.__qualname__}", "args": dict(tb.args)}
            for tb in tool_bindings
        ],
        "llm_round_count": llm_round_count,
        "repair_attempt_count": repair_attempt_count,
    }
