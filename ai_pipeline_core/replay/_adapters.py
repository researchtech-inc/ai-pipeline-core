"""Adapter dispatch for generic replay execution."""

import inspect
from collections.abc import Callable
from typing import Any

from ai_pipeline_core._codec import CodecImportError, import_by_path

__all__ = [
    "_invoke_callable",
    "resolve_callable",
]

_MISSING = object()


def _parse_target(target: str) -> tuple[str, str, str]:
    kind, separator, remainder = target.partition(":")
    if not separator or not kind or not remainder:
        raise ValueError(f"Replay target {target!r} is invalid. Use 'kind:module:qualname'.")
    module_name, separator, path = remainder.partition(":")
    if not separator or not module_name or not path:
        raise ValueError(f"Replay target {target!r} is invalid. Use 'kind:module:qualname'.")
    return kind, module_name, path


def _split_method_target(target: str, expected_kind: str) -> tuple[str, str]:
    kind, module_name, path = _parse_target(target)
    if kind != expected_kind:
        raise ValueError(f"Replay target {target!r} uses kind {kind!r}, expected {expected_kind!r}.")
    class_qualname, separator, method_name = path.rpartition(".")
    if not separator or not class_qualname or not method_name:
        raise ValueError(f"Replay target {target!r} must end with '.method_name'.")
    return f"{module_name}:{class_qualname}", method_name


def _filter_kwargs(callable_obj: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(callable_obj)
    if not signature.parameters:
        return arguments
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return arguments
    accepted = set(signature.parameters)
    return {key: value for key, value in arguments.items() if key in accepted}


async def _invoke_callable(callable_obj: Callable[..., Any], arguments: Any) -> Any:
    if arguments is _MISSING:
        result = callable_obj()
    elif isinstance(arguments, dict):
        result = callable_obj(**_filter_kwargs(callable_obj, arguments))
    elif isinstance(arguments, tuple | list):
        result = callable_obj(*arguments)
    else:
        raise TypeError(f"Replay arguments must decode to dict, tuple, list, or be omitted. Got {type(arguments).__name__}.")
    if inspect.isawaitable(result):
        return await result
    return result


def _construct_instance(class_path: str, receiver: Any) -> Any:
    if not isinstance(receiver, dict):
        raise TypeError(f"Replay receiver for {class_path} must decode to an object with mode/value fields.")
    if receiver.get("mode") != "constructor_args":
        raise ValueError(f"Replay receiver for {class_path} must use mode='constructor_args'.")
    constructor_args = receiver.get("value", _MISSING)
    cls = import_by_path(class_path)
    if not isinstance(cls, type):
        raise CodecImportError(f"Replay class path {class_path!r} resolved to {type(cls).__name__}, not a class.")
    if constructor_args is _MISSING:
        return cls()
    if isinstance(constructor_args, dict):
        return cls(**_filter_kwargs(cls, constructor_args))
    if isinstance(constructor_args, tuple):
        return cls(*constructor_args)
    if isinstance(constructor_args, list):
        return cls(*constructor_args)
    raise TypeError(f"Replay constructor args for {class_path} must decode to dict, tuple, list, or be omitted. Got {type(constructor_args).__name__}.")


def _load_class(class_path: str) -> type[Any]:
    cls = import_by_path(class_path)
    if not isinstance(cls, type):
        raise CodecImportError(f"Replay class path {class_path!r} resolved to {type(cls).__name__}, not a class.")
    return cls


def _resolve_target_method(target: str, *, expected_kind: str) -> tuple[type[Any], str]:
    class_path, method_name = _split_method_target(target, expected_kind)
    return _load_class(class_path), method_name


def _require_callable(target: str, callable_obj: Any, *, context: str) -> Callable[..., Any]:
    if not callable(callable_obj):
        raise TypeError(f"Replay target {target!r} did not resolve to a callable {context}.")
    return callable_obj


def _resolve_function_callable(target: str) -> Callable[..., Any]:
    _, module_name, path = _parse_target(target)
    callable_obj = import_by_path(f"{module_name}:{path}")
    return _require_callable(target, callable_obj, context="function")


def _resolve_classmethod_callable(target: str) -> Callable[..., Any]:
    cls, method_name = _resolve_target_method(target, expected_kind="classmethod")
    return _require_callable(target, getattr(cls, method_name), context="class method")


def _resolve_instance_method_callable(target: str, receiver: Any) -> Callable[..., Any]:
    class_path, method_name = _split_method_target(target, "instance_method")
    instance = _construct_instance(class_path, receiver)
    return _require_callable(target, getattr(instance, method_name), context="instance method")


def _bind_instance_method(target: str, cls: type[Any], instance: Any, method_name: str, *, context: str) -> Callable[..., Any]:
    if not isinstance(instance, cls):
        raise TypeError(f"Replay receiver for {target!r} reconstructed {type(instance).__name__}, expected {cls.__name__}.")
    return _require_callable(target, getattr(instance, method_name), context=context)


def _require_dict_receiver(target: str, receiver: Any) -> dict[str, Any]:
    if not isinstance(receiver, dict):
        raise TypeError(f"Replay receiver for {target!r} must decode to an object with mode/value fields.")
    return receiver


def _resolve_decoded_method_callable(target: str, receiver: Any) -> Callable[..., Any]:
    cls, method_name = _resolve_target_method(target, expected_kind="decoded_method")
    payload = _require_dict_receiver(target, receiver)
    if payload.get("mode") != "decoded_state":
        raise ValueError(f"Replay receiver for {target!r} must use mode='decoded_state'.")
    return _bind_instance_method(target, cls, payload.get("value"), method_name, context="decoded method")


def _resolve_promptcontract_receiver_callable(target: str, receiver: Any) -> Callable[..., Any]:
    cls, method_name = _resolve_target_method(target, expected_kind="decoded_promptcontract")
    payload = _require_dict_receiver(target, receiver)
    if payload.get("mode") != "promptcontract_receiver":
        raise ValueError(f"Replay receiver for {target!r} must use mode='promptcontract_receiver'.")
    value = payload.get("value")
    if not isinstance(value, dict):
        raise TypeError(f"Replay receiver for {target!r} must decode to {{'class_path', 'instance_fields'}}.")
    instance_fields = value.get("instance_fields", {})
    if not isinstance(instance_fields, dict):
        raise TypeError(f"Replay receiver for {target!r} 'instance_fields' must be a dict of field name -> value.")
    instance = cls(**_filter_kwargs(cls, instance_fields))
    return _bind_instance_method(target, cls, instance, method_name, context="prompt-contract receiver")


def resolve_callable(target: str, receiver: Any) -> Callable[..., Any]:
    """Resolve the live callable for a replay target string."""
    kind, _, _ = _parse_target(target)
    match kind:
        case "function":
            return _resolve_function_callable(target)
        case "classmethod":
            return _resolve_classmethod_callable(target)
        case "tool_call":
            raise ValueError(
                f"Replay target {target!r} is a tool_call span. "
                f"Tool-call spans are not individually replayable. "
                f"Replay the parent conversation span instead, passing override_tools= if needed."
            )
        case "instance_method":
            return _resolve_instance_method_callable(target, receiver)
        case "decoded_method":
            return _resolve_decoded_method_callable(target, receiver)
        case "decoded_promptcontract":
            return _resolve_promptcontract_receiver_callable(target, receiver)
        case _:
            raise ValueError(
                f"Replay target {target!r} is not supported. Use function, classmethod, instance_method, decoded_method, or decoded_promptcontract."
            )
