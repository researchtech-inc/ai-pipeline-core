"""JSON Schema helpers for OpenAI strict-mode requests."""

import copy
import json
from collections.abc import Iterator, Mapping
from enum import Enum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from .request import ListOf

JsonSchema = dict[str, Any]

# Pydantic ref_template placeholder. ``{model}`` is consumed by Pydantic itself
# (str.format), not by Python f-strings, so it stays literal here.
_PYDANTIC_REF_TEMPLATE = "#/$defs/" + "{model}"


def ensure_strict_schema(schema: JsonSchema, *, context: str) -> None:
    """Validate and mutate a JSON schema for OpenAI strict mode."""
    reject_dynamic_key_objects(schema, context=context)
    make_strict_schema(schema)


def schema_for_model(model: type[BaseModel]) -> JsonSchema:
    """Strict-mode JSON schema for a single Pydantic model."""
    schema = model.model_json_schema(ref_template=_PYDANTIC_REF_TEMPLATE)
    ensure_strict_schema(schema, context=f"Structured output {model.__name__}")
    return schema


def schema_for_list(inner: type[BaseModel]) -> JsonSchema:
    """Strict-mode JSON schema for ``list[inner]`` wrapped under ``items``."""
    item_schema = inner.model_json_schema(ref_template=_PYDANTIC_REF_TEMPLATE)
    definitions = item_schema.pop("$defs", None)
    schema: JsonSchema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item_schema}},
        "required": ["items"],
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = definitions
    ensure_strict_schema(schema, context=f"Structured output list[{inner.__name__}]")
    return schema


_SCHEMA_PROMPT_HEADER = (
    "## Response schema\n"
    "Return a JSON document matching the schema below. Field types are listed; "
    "required fields are marked. Do not add fields outside the schema."
)


def describe_schema_for_prompt(response_format: type[BaseModel] | ListOf) -> str:
    """Build a prose schema description + minimal example for prompt injection.

    Used when ``AIModel.supports_json_schema`` is ``False`` to coax models
    that ignore the wire-level ``response_format`` (or that the proxy
    downgraded from ``json_schema`` to ``json_object``) into returning a
    JSON document that still validates against the declared Pydantic shape.
    """
    if isinstance(response_format, ListOf):
        inner = response_format.inner
        body = _describe_model(inner, depth=0)
        example = {"items": [_example_for_model(inner)]}
        return (
            f"{_SCHEMA_PROMPT_HEADER}\n\n"
            f"Top-level object with a single field `items`: an array of `{inner.__name__}` objects.\n"
            f"`{inner.__name__}` fields:\n{body}\n\n"
            f"Example:\n```json\n{json.dumps(example, indent=2)}\n```"
        )
    body = _describe_model(response_format, depth=0)
    example = _example_for_model(response_format)
    return (
        f"{_SCHEMA_PROMPT_HEADER}\n\n"
        f"`{response_format.__name__}` fields:\n{body}\n\n"
        f"Example:\n```json\n{json.dumps(example, indent=2)}\n```"
    )


def _describe_model(model: type[BaseModel], *, depth: int) -> str:
    """Render one model's fields as an indented bullet list."""
    indent = "  " * depth
    lines: list[str] = []
    for field_name, field_info in model.model_fields.items():
        type_label = _annotation_label(field_info.annotation)
        required = "required" if field_info.is_required() else "optional"
        lines.append(f"{indent}- `{field_name}` ({type_label}, {required})")
        nested = _nested_model(field_info.annotation)
        if nested is not None:
            lines.append(_describe_model(nested, depth=depth + 1))
    return "\n".join(lines)


_PRIMITIVE_LABELS: dict[type, str] = {str: "string", bool: "boolean", int: "integer", float: "number"}


def _annotation_label(annotation: Any) -> str:
    """One-line type label for a field annotation."""
    if annotation is None or annotation is type(None):
        return "null"
    if isinstance(annotation, type):
        return _bare_type_label(annotation)
    origin = get_origin(annotation)
    if origin is Literal:
        values = ", ".join(repr(value) for value in get_args(annotation))
        return f"enum ({values})"
    if origin is list or origin is tuple:
        return _container_label(annotation, origin)
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else "value"


def _bare_type_label(annotation: type) -> str:
    """Render the label for an annotation that is a bare class (BaseModel/Enum/primitive)."""
    if issubclass(annotation, BaseModel):
        return f"object ({annotation.__name__})"
    if issubclass(annotation, Enum):
        values = ", ".join(repr(member.value) for member in annotation)
        return f"enum ({values})"
    return _PRIMITIVE_LABELS.get(annotation, getattr(annotation, "__name__", None) or "value")


def _container_label(annotation: Any, origin: Any) -> str:
    """Render array-style annotations (``list[T]`` / ``tuple[T, ...]``)."""
    args = get_args(annotation)
    if origin is list:
        (inner,) = args or (Any,)
        return f"array of {_annotation_label(inner)}"
    if len(args) == 2 and args[1] is Ellipsis:
        return f"array of {_annotation_label(args[0])}"
    return "array"


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """If the annotation directly wraps a BaseModel subclass, return it for recursion."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _example_for_model(model: type[BaseModel]) -> dict[str, Any]:
    """Build a minimal example object from a Pydantic model."""
    return {name: _example_for_annotation(info.annotation) for name, info in model.model_fields.items()}


_PRIMITIVE_EXAMPLES: dict[type, Any] = {bool: False, int: 0, float: 0.0, str: ""}


def _example_for_annotation(annotation: Any) -> Any:
    """Pick a placeholder value matching the field annotation."""
    if isinstance(annotation, type):
        return _bare_type_example(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return args[0] if args else ""
    if origin is list:
        (inner,) = args or (Any,)
        return [_example_for_annotation(inner)]
    if origin is tuple:
        return [_example_for_annotation(args[0])] if args else []
    return ""


def _bare_type_example(annotation: type) -> Any:
    """Render an example value for a bare class annotation (BaseModel/Enum/primitive)."""
    if issubclass(annotation, BaseModel):
        return _example_for_model(annotation)
    if issubclass(annotation, Enum):
        first = next(iter(annotation), None)
        return first.value if first is not None else ""
    return _PRIMITIVE_EXAMPLES.get(annotation, "")


def resolve_local_ref(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve a ``#/$defs/...`` reference against ``root``; pass through if absent."""
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return schema
    definitions = root.get("$defs")
    if not isinstance(definitions, Mapping):
        return schema
    resolved = definitions.get(ref.removeprefix("#/$defs/"))
    return resolved if isinstance(resolved, Mapping) else schema


def reject_dynamic_key_objects(schema: JsonSchema, *, context: str) -> None:
    """Reject ``dict[str, V]`` schemas before strict-mode mutation corrupts them."""
    _reject_dynamic_key_objects(schema, context, context)


def make_strict_schema(schema: JsonSchema) -> None:
    """Recursively add strict-mode object requirements to a JSON schema."""
    _inline_ref_siblings(schema, schema)
    _make_strict_schema(schema)


def _make_strict_schema(schema: JsonSchema) -> None:
    """Apply strict-mode mutation after provider-incompatible refs are normalized."""
    _apply_object_strictness(schema)
    for _path, child_schema in _child_schemas(schema):
        _make_strict_schema(child_schema)


def _inline_ref_siblings(schema: JsonSchema, root: JsonSchema) -> None:
    """Inline ``$ref`` schemas that also carry sibling metadata."""
    ref = schema.get("$ref")
    if isinstance(ref, str) and len(schema) > 1:
        resolved = _resolve_local_ref(root, ref)
        if resolved is not None:
            siblings = {key: value for key, value in schema.items() if key != "$ref"}
            schema.clear()
            schema.update(copy.deepcopy(resolved))
            schema.update(siblings)
    for _path, child_schema in _child_schemas(schema):
        _inline_ref_siblings(child_schema, root)


def _resolve_local_ref(root: JsonSchema, ref: str) -> JsonSchema | None:
    """Resolve a local ``#/$defs/...`` JSON schema reference."""
    if not ref.startswith("#/$defs/"):
        return None
    definitions = root.get("$defs")
    if not isinstance(definitions, dict):
        return None
    value = definitions.get(ref.removeprefix("#/$defs/"))
    return value if isinstance(value, dict) else None


def _apply_object_strictness(schema: JsonSchema) -> None:
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        properties = schema.get("properties")
        if isinstance(properties, dict):
            schema["required"] = [str(key) for key in properties]


def _reject_dynamic_key_objects(node: JsonSchema, context: str, path: str) -> None:
    if (
        node.get("type") == "object"
        and isinstance(node.get("additionalProperties"), dict)
        and not node.get("properties")
    ):
        raise TypeError(
            f"{context} has a dict type at '{path}' which is incompatible with OpenAI strict mode. "
            "dict[str, V] produces dynamic-key objects that cannot be represented in strict-mode JSON schemas. "
            "Replace it with list[SomeModel] where SomeModel has explicit fields."
        )

    for child_path, child_schema in _child_schemas(node):
        _reject_dynamic_key_objects(child_schema, context, f"{path}.{child_path}")


def _child_schemas(node: JsonSchema) -> Iterator[tuple[str, JsonSchema]]:
    properties = node.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict):
                yield f"{prop_name}", prop_schema

    definitions = node.get("$defs")
    if isinstance(definitions, dict):
        for def_name, def_schema in definitions.items():
            if isinstance(def_schema, dict):
                yield f"$defs.{def_name}", def_schema

    for key in ("allOf", "anyOf", "oneOf"):
        branch_items = node.get(key)
        if isinstance(branch_items, list):
            for index, item in enumerate(branch_items):
                if isinstance(item, dict):
                    yield f"{key}[{index}]", item

    items = node.get("items")
    if isinstance(items, dict):
        yield "items", items
