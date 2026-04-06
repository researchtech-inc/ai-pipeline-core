"""Type validation helpers for pipeline task and flow annotations."""

__all__ = [
    "callable_name",
    "collect_document_types",
    "contains_bare_document",
    "flatten_union",
    "is_already_traced",
    "is_optional_annotation",
    "resolve_type_hints",
    "unwrap_optional",
    "validate_flow_input_param",
    "validate_task_argument_value",
    "validate_task_input_annotation",
    "validate_task_return_annotation",
]

import types
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._conversation import is_conversation_instance, is_conversation_type

ALLOWED_TASK_SCALAR_TYPES: tuple[type[Any], ...] = (str, int, float, bool)
_MAX_WRAPPED_DEPTH = 10
_UNHANDLED = object()
type _RuntimeValidationResult = object | str | None


def callable_name(obj: Any, fallback: str) -> str:
    """Safely extract a callable name for error messages."""
    try:
        name = getattr(obj, "__name__", None)
    except Exception:
        return fallback
    return name if isinstance(name, str) else fallback


def is_already_traced(func: Callable[..., Any]) -> bool:
    """Check whether a function was already wrapped by the trace decorator."""
    if hasattr(func, "__is_traced__") and func.__is_traced__:  # type: ignore[attr-defined]
        return True

    current = func
    depth = 0
    while hasattr(current, "__wrapped__") and depth < _MAX_WRAPPED_DEPTH:
        wrapped = current.__wrapped__  # type: ignore[attr-defined]
        if hasattr(wrapped, "__is_traced__") and wrapped.__is_traced__:
            return True
        current = wrapped
        depth += 1

    return False


def _unwrap_annotated(tp: Any) -> Any:
    """Strip Annotated metadata from a type annotation."""
    while get_origin(tp) is Annotated:
        args = get_args(tp)
        if not args:
            break
        tp = args[0]
    return tp


def _is_none_type(tp: Any) -> bool:
    """Check whether a type annotation represents None."""
    return _unwrap_annotated(tp) is type(None)


def _is_union_annotation(tp: Any) -> bool:
    """Check whether an annotation is a ``Union`` or ``X | Y``."""
    unwrapped = _unwrap_annotated(tp)
    origin = get_origin(unwrapped)
    return origin is Union or isinstance(unwrapped, types.UnionType)


def flatten_union(tp: Any) -> list[Any]:
    """Flatten ``Union`` and ``X | Y`` annotations into their leaf members."""
    unwrapped = _unwrap_annotated(tp)
    if not _is_union_annotation(unwrapped):
        return [unwrapped]

    flattened: list[Any] = []
    for arg in get_args(unwrapped):
        flattened.extend(flatten_union(arg))
    return flattened


def unwrap_optional(annotation: Any) -> Any:
    """Unwrap ``Optional[X]`` / ``X | None`` to ``X``."""
    if _is_union_annotation(annotation):
        non_none_members = [member for member in flatten_union(annotation) if not _is_none_type(member)]
        if len(non_none_members) == 1:
            return non_none_members[0]
    return annotation


def is_optional_annotation(annotation: Any) -> bool:
    """Check whether an annotation is ``Optional[X]`` / ``X | None``."""
    if not _is_union_annotation(annotation):
        return False
    return any(_is_none_type(member) for member in flatten_union(annotation))


def contains_bare_document(tp: Any) -> bool:
    """Check whether an annotation contains the bare ``Document`` class."""
    unwrapped = _unwrap_annotated(tp)
    if unwrapped is Document:
        return True
    if _is_none_type(unwrapped):
        return False
    if _is_union_annotation(unwrapped):
        return any(contains_bare_document(arg) for arg in get_args(unwrapped))

    origin = get_origin(unwrapped)
    if origin in {dict, list, tuple}:
        return any(contains_bare_document(arg) for arg in get_args(unwrapped) if arg is not Ellipsis)

    return False


def collect_document_types(annotation: Any) -> list[type[Document]]:
    """Collect concrete Document subclasses referenced anywhere in an annotation."""
    unwrapped = _unwrap_annotated(annotation)
    if _is_none_type(unwrapped):
        return []
    if _is_document_subclass(unwrapped):
        return [unwrapped]
    if _is_union_annotation(unwrapped):
        return _collect_nested_document_types(get_args(unwrapped))

    origin = get_origin(unwrapped)
    if origin in {dict, list, tuple}:
        args = [arg for arg in get_args(unwrapped) if arg is not Ellipsis]
        return _collect_nested_document_types(args)

    return []


def _collect_nested_document_types(annotations: tuple[Any, ...] | list[Any]) -> list[type[Document]]:
    document_types: dict[str, type[Document]] = {}
    for annotation in annotations:
        for document_type in collect_document_types(annotation):
            document_types[document_type.__name__] = document_type
    return list(document_types.values())


def _is_literal_annotation(annotation: Any) -> bool:
    """Check whether the annotation is a ``Literal``."""
    return get_origin(_unwrap_annotated(annotation)) is Literal


def _literal_values_are_supported(annotation: Any) -> bool:
    """Check whether all literal values use supported scalar runtime types."""
    return all(isinstance(value, ALLOWED_TASK_SCALAR_TYPES) for value in get_args(_unwrap_annotated(annotation)))


def _is_conversation_annotation(annotation: Any) -> bool:
    """Check whether an annotation is ``Conversation`` or ``Conversation[T]``."""
    unwrapped = _unwrap_annotated(annotation)
    origin = get_origin(unwrapped)
    return is_conversation_type(unwrapped) or is_conversation_type(origin)


def _is_document_subclass(annotation: Any) -> bool:
    """Check whether an annotation is a concrete ``Document`` subclass."""
    return isinstance(annotation, type) and issubclass(annotation, Document) and annotation is not Document


def _is_frozen_basemodel_subclass(annotation: Any) -> bool:
    """Check whether an annotation is a frozen Pydantic model subclass."""
    unwrapped = _unwrap_annotated(annotation)
    if not isinstance(unwrapped, type) or not issubclass(unwrapped, BaseModel) or issubclass(unwrapped, Document):
        return False

    model_config = cast(Mapping[str, Any], getattr(unwrapped, "model_config", {}))
    return bool(model_config.get("frozen"))


def _is_supported_leaf_task_input(annotation: Any) -> bool:
    """Check whether an annotation is one supported non-container task input leaf type."""
    unwrapped = _unwrap_annotated(annotation)
    if unwrapped in ALLOWED_TASK_SCALAR_TYPES:
        return True
    if _is_conversation_annotation(unwrapped):
        return True
    if _is_literal_annotation(unwrapped):
        return _literal_values_are_supported(unwrapped)
    if isinstance(unwrapped, type) and issubclass(unwrapped, Enum):
        return True
    if _is_document_subclass(unwrapped):
        return True
    return _is_frozen_basemodel_subclass(unwrapped)


def _common_task_annotation_error(annotation: Any, *, context: str) -> str | None:
    """Return a shared error for invalid ``Any`` or bare ``Document`` usage."""
    if annotation is Any:
        return (
            "must not use 'Any'. Use a concrete scalar, Enum, frozen BaseModel, Conversation, "
            f"Document subclass, or a container of those types in task {context} annotations."
        )
    if contains_bare_document(annotation):
        message = "must not use bare 'Document'. "
        if context == "input":
            return message + "Use specific Document subclasses in task input annotations."
        return message + "Return specific Document subclasses."
    return None


def _union_annotation_error(annotation: Any, validator: Callable[[Any], str | None]) -> str | None:
    """Validate each branch of a union annotation."""
    for branch in flatten_union(annotation):
        if branch is type(None):
            continue
        if error := validator(branch):
            return error
    return None


def _contains_document_subclass(annotation: Any) -> bool:
    """Check whether an annotation contains any concrete Document subclass."""
    unwrapped = _unwrap_annotated(annotation)
    if _is_document_subclass(unwrapped):
        return True
    if _is_union_annotation(unwrapped):
        return any(_contains_document_subclass(arg) for arg in get_args(unwrapped))
    return False


def _input_list_annotation_error(annotation: Any) -> str | None:
    args = get_args(annotation)
    if len(args) != 1:
        return "must annotate list inputs as list[T] with exactly one element type."
    if _contains_document_subclass(args[0]):
        return (
            "must not use list[Document] for task inputs — lists are mutable and bypass pipeline immutability guarantees. "
            "Use tuple[DocumentSubclass, ...] instead."
        )
    return _task_input_annotation_error(args[0])


def _input_tuple_annotation_error(annotation: Any) -> str | None:
    args = get_args(annotation)
    if not args:
        return "must annotate tuple inputs as tuple[T, ...] or tuple[T1, T2, ...]."
    if len(args) == 2 and args[1] is Ellipsis:
        return _task_input_annotation_error(args[0])

    for arg in args:
        if error := _task_input_annotation_error(arg):
            return error
    return None


def _input_dict_annotation_error(annotation: Any) -> str | None:
    args = get_args(annotation)
    if len(args) != 2:
        return "must annotate dict inputs as dict[str, T]."

    key_type, value_type = args
    if _unwrap_annotated(key_type) is not str:
        return "must use dict[str, T] for task input mappings. Non-string keys are not supported."
    return _task_input_annotation_error(value_type)


def _unsupported_input_annotation_error(annotation: Any) -> str:
    if annotation is list:
        return "uses bare 'list' without a type parameter. Use list[str], list[int], or tuple[DocumentSubclass, ...] instead."
    if annotation is dict:
        return "uses bare 'dict' without a type parameter. Use dict[str, ValueType] instead."
    return (
        f"uses unsupported input annotation {annotation!r}. Use scalars, Enums, Conversation, specific Document subclasses, "
        "frozen BaseModel subclasses, or list/tuple/dict[str, ...] containers of those types."
    )


def _task_input_annotation_error(annotation: Any) -> str | None:
    """Return an error string for an invalid task input annotation."""
    unwrapped = _unwrap_annotated(annotation)
    if error := _common_task_annotation_error(unwrapped, context="input"):
        return error
    if _is_none_type(unwrapped) or _is_supported_leaf_task_input(unwrapped):
        return None
    if _is_union_annotation(unwrapped):
        return _union_annotation_error(unwrapped, _task_input_annotation_error)

    origin = get_origin(unwrapped)
    handler = {
        list: _input_list_annotation_error,
        tuple: _input_tuple_annotation_error,
        dict: _input_dict_annotation_error,
    }.get(origin)
    if handler is not None:
        return handler(unwrapped)
    return _unsupported_input_annotation_error(unwrapped)


def validate_task_input_annotation(annotation: Any, *, task_name: str, parameter_name: str) -> list[type[Document]]:
    """Validate a task input annotation and return any referenced Document types."""
    if error := _task_input_annotation_error(annotation):
        raise TypeError(f"PipelineTask '{task_name}'.run parameter '{parameter_name}' {error}")
    return collect_document_types(annotation)


def validate_flow_input_param(
    annotation: Any,
    *,
    flow_name: str,
    parameter_name: str,
) -> list[type[Document]]:
    """Validate a single ``PipelineFlow.run()`` input parameter annotation."""
    unwrapped = _unwrap_annotated(annotation)
    if contains_bare_document(unwrapped):
        raise TypeError(f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' uses bare 'Document'. Use a specific Document subclass.")
    if unwrapped is Any:
        raise TypeError(f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' uses 'Any'. Use a specific Document subclass or FlowOptions subclass.")
    optional_member = unwrap_optional(unwrapped)
    if optional_member is not unwrapped:
        if _is_document_subclass(optional_member):
            return [optional_member]
        raise TypeError(
            f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' "
            f"has annotation {annotation!r}. Optional flow inputs must use "
            "Optional[DocumentSubclass] / DocumentSubclass | None."
        )
    if _is_document_subclass(unwrapped):
        return [unwrapped]

    if get_origin(unwrapped) is tuple:
        args = get_args(unwrapped)
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError(
                f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' "
                f"has annotation {annotation!r}. Flow document collections must use "
                "tuple[DocumentSubclass, ...] or tuple[DocA | DocB, ...]."
            )
        member_annotation = _unwrap_annotated(args[0])
        if contains_bare_document(member_annotation):
            raise TypeError(
                f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' uses bare 'Document' in a tuple collection. Use specific Document subclasses."
            )
        member_types = collect_document_types(member_annotation)
        if not member_types or any(not _is_document_subclass(branch) for branch in flatten_union(member_annotation)):
            raise TypeError(
                f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' "
                f"has annotation {annotation!r}. Flow document collections must contain only "
                "specific Document subclasses."
            )
        return member_types

    raise TypeError(
        f"PipelineFlow '{flow_name}'.run parameter '{parameter_name}' "
        f"has annotation {annotation!r} which is not a supported flow input shape. "
        "Use DocumentSubclass, Optional[DocumentSubclass], tuple[DocumentSubclass, ...], "
        "tuple[DocA | DocB, ...], or a FlowOptions subclass."
    )


def _document_collection_error(annotation: Any) -> str | None:
    """Return an error for an invalid document collection member annotation."""
    unwrapped = _unwrap_annotated(annotation)
    if contains_bare_document(unwrapped):
        return "must not use bare 'Document'. Return specific Document subclasses."
    for branch in flatten_union(unwrapped):
        if not _is_document_subclass(branch):
            return f"uses unsupported output member annotation {branch!r}. Collections must contain only specific Document subclasses."
    return None


def _return_list_annotation_error(annotation: Any) -> str | None:
    args = get_args(annotation)
    if len(args) != 1:
        return "must use list[DocumentSubclass | ...] for list outputs."
    return _document_collection_error(args[0])


def _return_tuple_annotation_error(annotation: Any) -> str | None:
    args = get_args(annotation)
    if not args:
        return "must use tuple[DocumentSubclass, ...] or tuple[DocA, DocB, ...] for tuple outputs."
    if len(args) == 2 and args[1] is Ellipsis:
        return _document_collection_error(args[0])

    for arg in args:
        if error := _document_collection_error(arg):
            return error
    return None


def _task_return_annotation_error(annotation: Any) -> str | None:
    """Return an error string for an invalid task return annotation."""
    unwrapped = _unwrap_annotated(annotation)
    if error := _common_task_annotation_error(unwrapped, context="output"):
        return error
    if _is_none_type(unwrapped):
        return None
    if _is_document_subclass(unwrapped):
        return None
    if _is_union_annotation(unwrapped):
        return _union_annotation_error(unwrapped, _task_return_annotation_error)

    origin = get_origin(unwrapped)
    handler = {list: _return_list_annotation_error, tuple: _return_tuple_annotation_error}.get(origin)
    if handler is not None:
        return handler(unwrapped)
    return "must return a specific Document subclass, None, list[DocumentSubclass], tuple[DocumentSubclass, ...], or unions of those shapes."


def validate_task_return_annotation(annotation: Any, *, task_name: str) -> list[type[Document]]:
    """Validate a task return annotation and return referenced Document types."""
    if error := _task_return_annotation_error(annotation):
        raise TypeError(f"PipelineTask '{task_name}'.run {error}")
    output_types = collect_document_types(annotation)
    if not output_types and _unwrap_annotated(annotation) is not type(None):
        raise TypeError(
            f"PipelineTask '{task_name}'.run must return a specific Document subclass, None, "
            "list[DocumentSubclass], tuple[DocumentSubclass, ...], or unions of those shapes."
        )
    return output_types


def _matches_scalar(annotation: Any, value: Any) -> bool:
    """Check a scalar runtime value against a scalar annotation."""
    if annotation is bool:
        return type(value) is bool
    if annotation is int:
        return type(value) is int
    return isinstance(value, annotation)


def _runtime_none_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if not _is_none_type(annotation):
        return _UNHANDLED
    if value is None:
        return None
    return f"{path} must be None, got {type(value).__name__}"


def _literal_runtime_error(value: Any, annotation: Any, *, path: str) -> str | None:
    literal_values = get_args(annotation)
    if value in literal_values:
        return None
    return f"{path} must be one of {literal_values!r}, got {value!r}"


def _conversation_runtime_error(value: Any, *, path: str) -> str | None:
    if is_conversation_instance(value):
        return None
    return f"{path} must be Conversation, got {type(value).__name__}"


def _typed_leaf_runtime_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if not isinstance(annotation, type):
        return _UNHANDLED
    if issubclass(annotation, Enum) or issubclass(annotation, BaseModel):
        if isinstance(value, annotation):
            return None
        return f"{path} must be {annotation.__name__}, got {type(value).__name__}"
    if _matches_scalar(annotation, value):
        return None
    return f"{path} must be {annotation.__name__}, got {type(value).__name__}"


def _runtime_leaf_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if not _is_supported_leaf_task_input(annotation):
        return _UNHANDLED
    if _is_literal_annotation(annotation):
        return _literal_runtime_error(value, annotation, path=path)
    if _is_conversation_annotation(annotation):
        return _conversation_runtime_error(value, path=path)
    return _typed_leaf_runtime_error(value, annotation, path=path)


def _runtime_union_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if not _is_union_annotation(annotation):
        return _UNHANDLED
    for branch in flatten_union(annotation):
        if _runtime_value_error(value, branch, path=path) is None:
            return None
    return f"{path} does not match any allowed type in {annotation!r} (got {type(value).__name__})"


def _runtime_list_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if get_origin(annotation) is not list:
        return _UNHANDLED
    if not isinstance(value, list):
        return f"{path} must be list, got {type(value).__name__}"

    args = get_args(annotation)
    if len(args) != 1:
        return f"{path} uses unsupported annotation {annotation!r}"

    item_annotation = args[0]
    for index, item in enumerate(cast(list[Any], value)):
        if error := _runtime_value_error(item, item_annotation, path=f"{path}[{index}]"):
            return error
    return None


def _runtime_variadic_tuple_error(value: tuple[Any, ...], item_annotation: Any, *, path: str) -> str | None:
    for index, item in enumerate(value):
        if error := _runtime_value_error(item, item_annotation, path=f"{path}[{index}]"):
            return error
    return None


def _runtime_tuple_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if get_origin(annotation) is not tuple:
        return _UNHANDLED
    if not isinstance(value, tuple):
        return f"{path} must be tuple, got {type(value).__name__}"

    args = get_args(annotation)
    typed_value = cast(tuple[Any, ...], value)
    if len(args) == 2 and args[1] is Ellipsis:
        return _runtime_variadic_tuple_error(typed_value, args[0], path=path)
    if len(typed_value) != len(args):
        return f"{path} must contain {len(args)} items, got {len(typed_value)}"
    for index, (item, item_annotation) in enumerate(zip(typed_value, args, strict=True)):
        if error := _runtime_value_error(item, item_annotation, path=f"{path}[{index}]"):
            return error
    return None


def _runtime_dict_error(value: Any, annotation: Any, *, path: str) -> _RuntimeValidationResult:
    if get_origin(annotation) is not dict:
        return _UNHANDLED
    if not isinstance(value, dict):
        return f"{path} must be dict, got {type(value).__name__}"

    args = get_args(annotation)
    if len(args) != 2:
        return f"{path} uses unsupported annotation {annotation!r}"

    key_annotation, value_annotation = args
    for key, item in cast(dict[Any, Any], value).items():
        if error := _runtime_value_error(key, key_annotation, path=f"{path}.keys()"):
            return error
        key_repr = key if isinstance(key, str) else repr(key)
        if error := _runtime_value_error(item, value_annotation, path=f"{path}[{key_repr!r}]"):
            return error
    return None


def _runtime_value_error(value: Any, annotation: Any, *, path: str) -> str | None:
    """Return an error string if a runtime value does not match an annotation."""
    unwrapped = _unwrap_annotated(annotation)

    for validator in (_runtime_none_error, _runtime_leaf_error, _runtime_union_error, _runtime_list_error, _runtime_tuple_error, _runtime_dict_error):
        error = validator(value, unwrapped, path=path)
        if error is not _UNHANDLED:
            return cast(str | None, error)

    return f"{path} uses unsupported annotation {unwrapped!r}"


def validate_task_argument_value(*, task_name: str, parameter_name: str, value: Any, annotation: Any) -> None:
    """Validate a runtime task argument against its declared annotation."""
    if error := _runtime_value_error(value, annotation, path=parameter_name):
        raise TypeError(f"PipelineTask '{task_name}.run' received invalid value for '{parameter_name}'. {error}. Expected annotation: {annotation!r}.")


def resolve_type_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Resolve type hints, raising ``TypeError`` with the original cause."""
    try:
        return get_type_hints(fn, include_extras=True)
    except Exception as exc:
        name = callable_name(fn, "unknown")
        raise TypeError(f"Failed to resolve type hints for '{name}': {exc}") from exc
