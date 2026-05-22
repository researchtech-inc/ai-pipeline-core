"""Regression tests for Tool.Input schema incompatibilities."""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm.tools import Tool


def test_tool_definition_rejects_dict_field() -> None:
    """dict[str, V] produces dynamic-key objects incompatible with OpenAI strict mode.

    Tool.__init_subclass__ should reject dict fields at import time.
    """

    def define_tool() -> type[Tool]:
        class DictTool(Tool):
            """Tool with dict field."""

            class Input(BaseModel):
                coins: dict[str, list[int]] = Field(description="coin -> timestamps mapping")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return DictTool

    with pytest.raises(TypeError, match=r"dict type.*incompatible"):
        define_tool()


def test_tool_definition_rejects_nested_dict_in_list() -> None:
    """dict[str, V] nested inside list[...] is also incompatible with strict mode."""

    def define_tool() -> type[Tool]:
        class NestedDictTool(Tool):
            """Tool with nested dict in list."""

            class Input(BaseModel):
                items: list[dict[str, str]] = Field(description="list of dicts")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return NestedDictTool

    with pytest.raises(TypeError, match="dict type"):
        define_tool()


def test_tool_definition_rejects_dict_in_referenced_model() -> None:
    """dict[str, V] inside a referenced model (appears in $defs) must also be rejected."""

    class ItemData(BaseModel):
        values: dict[str, int] = Field(description="key-value data")

    def define_tool() -> type[Tool]:
        class RefDictTool(Tool):
            """Tool with dict in referenced model."""

            class Input(BaseModel):
                item: ItemData = Field(description="item with dict")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return RefDictTool

    with pytest.raises(TypeError, match="dict type"):
        define_tool()


def test_tool_definition_rejects_optional_dict_field() -> None:
    """dict[str, V] | None wraps in anyOf — the dict branch must still be rejected."""

    def define_tool() -> type[Tool]:
        class OptionalDictTool(Tool):
            """Tool with optional dict field."""

            class Input(BaseModel):
                data: dict[str, int] | None = Field(description="optional dict", default=None)

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return OptionalDictTool

    with pytest.raises(TypeError, match="dict type"):
        define_tool()


def test_tool_definition_allows_nested_basemodel() -> None:
    """Nested BaseModel with fixed properties remains valid."""

    class Entry(BaseModel):
        key: str = Field(description="key")
        value: int = Field(description="value")

    class GoodTool(Tool):
        """Tool with explicit nested model."""

        class Input(BaseModel):
            entries: list[Entry] = Field(description="entries")

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

    assert issubclass(GoodTool.Input, BaseModel)


def test_strict_schema_rejects_dict_field_before_mutation() -> None:
    """Reject dict[str, V] before strict-mode mutation corrupts the schema.

    This protects every strict-schema caller, even when a schema is built outside
    Tool.__init_subclass__ validation.
    """

    # Bypass __init_subclass__ validation by building schema directly from a plain model
    class DictInput(BaseModel):
        coins: dict[str, list[int]] = Field(description="coin timestamps")
        search_width: str | None = Field(description="search width", default=None)

    schema = DictInput.model_json_schema()
    # Before strict: coins has additionalProperties (dict schema) and no properties
    coins_before = schema["properties"]["coins"]
    assert "additionalProperties" in coins_before
    assert "properties" not in coins_before

    from ai_pipeline_core._llm_core._strict_schema import ensure_strict_schema

    with pytest.raises(TypeError, match="dict type"):
        ensure_strict_schema(schema, context="DictInput")


def test_tool_definition_rejects_reserved_field_strict() -> None:
    """Field named 'strict' collides with LiteLLM recursive key stripping."""

    def define_tool() -> type[Tool]:
        class StrictTool(Tool):
            """Tool with reserved field name."""

            class Input(BaseModel):
                strict: bool = Field(description="strict mode", default=True)

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return StrictTool

    with pytest.raises(TypeError, match="reserved name"):
        define_tool()


def test_tool_definition_rejects_reserved_field_additional_properties() -> None:
    """Field named 'additionalProperties' collides with JSON Schema keyword."""

    def define_tool() -> type[Tool]:
        class APTool(Tool):
            """Tool with additionalProperties field."""

            class Input(BaseModel):
                additionalProperties: bool = Field(description="flag")

            class Output(BaseModel):
                result: str

            async def run(self, input: Input) -> Output:
                return self.Output(result="")

        return APTool

    with pytest.raises(TypeError, match="reserved name"):
        define_tool()


def test_tool_definition_allows_renamed_reserved_field() -> None:
    """Renamed variant of reserved name is valid."""

    class GoodTool(Tool):
        """Tool with renamed field."""

        class Input(BaseModel):
            strict_mode: bool = Field(description="Enable strict mode", default=True)

        class Output(BaseModel):
            result: str

        async def run(self, input: Input) -> Output:
            return self.Output(result="")

    assert "strict_mode" in GoodTool.Input.model_fields


def test_strict_field_collides_in_generated_schema() -> None:
    """Prove the schema collision: user field 'strict' appears inside properties
    at the same level as function-level 'strict': True.

    LiteLLM strips all 'strict' keys recursively — removing the user's property
    but leaving its name in 'required', causing provider rejection.
    """

    # Build schema manually from a plain model (bypassing __init_subclass__)
    class StrictInput(BaseModel):
        strict: bool = Field(description="strict mode", default=True)
        query: str = Field(description="search query")

    schema = StrictInput.model_json_schema()
    from ai_pipeline_core._llm_core._strict_schema import make_strict_schema

    make_strict_schema(schema)

    # The generated tool envelope would have:
    tool_schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": "test",
            "parameters": schema,
            "strict": True,  # function-level strict
        },
    }
    # Both function-level "strict" and property-level "strict" exist
    assert "strict" in tool_schema["function"]  # function-level
    assert "strict" in tool_schema["function"]["parameters"]["properties"]
    # LiteLLM would strip BOTH, leaving 'strict' in required but missing from properties
    assert "strict" in tool_schema["function"]["parameters"]["required"]


def test_strict_schema_inlines_ref_siblings_for_provider_compatibility() -> None:
    """Strict schemas preserve field descriptions without putting siblings beside $ref."""

    class ChildModel(BaseModel):
        value: str = Field(description="Child value")

    class ParentModel(BaseModel):
        child: ChildModel = Field(description="Child field")

    from ai_pipeline_core._llm_core._strict_schema import ensure_strict_schema

    schema = ParentModel.model_json_schema(ref_template="#/$defs/{model}")
    ensure_strict_schema(schema, context="ParentModel")

    child_property = schema["properties"]["child"]
    assert "$ref" not in child_property
    assert child_property["description"] == "Child field"
    assert child_property["type"] == "object"
    assert child_property["properties"]["value"]["description"] == "Child value"
