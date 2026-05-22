"""JSON Schema helpers for OpenAI strict-mode requests."""

import copy
from collections.abc import Iterator, Mapping
from typing import Any

from pydantic import BaseModel

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
