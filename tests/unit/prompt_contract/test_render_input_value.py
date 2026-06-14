"""Tests for ``render_input_value`` type dispatch (Stage A item 6).

The helper lives in ``prompt_contract.render`` so the renderer module can
reuse it. The dispatch behavior is unchanged.
"""

import logging
from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel

from ai_pipeline_core.prompt_contract.render import render_input_value as _render_input_value


class TestStrEnum:
    def test_str_enum_renders_value(self) -> None:
        class Color(StrEnum):
            RED = "red"
            BLUE = "blue"

        assert _render_input_value(Color.RED) == "red"
        assert _render_input_value(Color.BLUE) == "blue"


class TestLiteralAndScalars:
    def test_literal_string_renders_directly(self) -> None:
        # Literal values reach the helper as plain strings.
        value: Literal["initial", "follow_up"] = "initial"
        assert _render_input_value(value) == "initial"

    def test_string_unchanged(self) -> None:
        assert _render_input_value("hello world") == "hello world"

    def test_bool_renders_via_str(self) -> None:
        assert _render_input_value(True) == "True"
        assert _render_input_value(False) == "False"

    def test_int_renders_via_str(self) -> None:
        assert _render_input_value(42) == "42"
        assert _render_input_value(0) == "0"

    def test_float_renders_via_str(self) -> None:
        assert _render_input_value(3.14) == "3.14"


class TestBaseModel:
    def test_base_model_renders_as_indented_json(self) -> None:
        class M(BaseModel):
            x: int
            y: str

        rendered = _render_input_value(M(x=1, y="hello"))
        assert '"x": 1' in rendered
        assert '"y": "hello"' in rendered
        # Indented format
        assert "\n" in rendered


class TestCollections:
    def test_list_of_models_renders_as_json_array(self) -> None:
        class Item(BaseModel):
            n: int

        rendered = _render_input_value([Item(n=1), Item(n=2)])
        assert rendered.startswith("[\n")
        assert rendered.endswith("\n]")
        assert '"n": 1' in rendered
        assert '"n": 2' in rendered

    def test_tuple_of_models_renders_as_json_array(self) -> None:
        class Item(BaseModel):
            n: int

        rendered = _render_input_value((Item(n=1), Item(n=2)))
        assert '"n": 1' in rendered
        assert '"n": 2' in rendered

    def test_list_of_primitives_renders_as_bullet_list(self) -> None:
        rendered = _render_input_value(["alpha", "beta", "gamma"])
        assert rendered == "- alpha\n- beta\n- gamma"

    def test_tuple_of_primitives_renders_as_bullet_list(self) -> None:
        rendered = _render_input_value(("x", "y"))
        assert rendered == "- x\n- y"

    def test_empty_collection_renders_as_empty_array(self) -> None:
        assert _render_input_value([]) == "[]"
        assert _render_input_value(()) == "[]"

    def test_mixed_collection_falls_back_to_repr(self) -> None:
        class M(BaseModel):
            n: int

        rendered = _render_input_value([M(n=1), "string"])
        # repr() output of the list — exact form not asserted, just contains a fallback shape.
        assert "string" in rendered


class TestNone:
    def test_none_renders_as_placeholder(self) -> None:
        assert _render_input_value(None) == "(none)"


class TestUnknownType:
    def test_unknown_type_falls_back_to_repr(self) -> None:
        class Custom:
            def __repr__(self) -> str:
                return "Custom-instance"

        assert _render_input_value(Custom()) == "Custom-instance"

    def test_unknown_type_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        class Custom:
            def __repr__(self) -> str:
                return "Custom"

        with caplog.at_level(logging.WARNING, logger="ai_pipeline_core.prompt_contract.render"):
            _render_input_value(Custom())
        assert any("unsupported type" in record.message for record in caplog.records)


class TestLiteralEdgeCases:
    """Numeric and mixed-type Literal values reach the helper as primitives."""

    def test_numeric_literal(self) -> None:
        value: Literal[1, 2, 3] = 2
        assert _render_input_value(value) == "2"

    def test_mixed_string_int_literal_string_value(self) -> None:
        value: Literal["x", 1] = "x"
        assert _render_input_value(value) == "x"

    def test_mixed_string_int_literal_int_value(self) -> None:
        value: Literal["x", 1] = 1
        assert _render_input_value(value) == "1"


class TestStrEnumCollections:
    """Tuples/lists of StrEnum values render as bullet lists of underlying strings."""

    def test_tuple_of_str_enum_values(self) -> None:
        class Color(StrEnum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"

        rendered = _render_input_value((Color.RED, Color.GREEN, Color.BLUE))
        assert rendered == "- red\n- green\n- blue"

    def test_list_of_str_enum_values(self) -> None:
        class Status(StrEnum):
            OPEN = "open"
            CLOSED = "closed"

        rendered = _render_input_value([Status.OPEN, Status.CLOSED])
        assert rendered == "- open\n- closed"

    def test_mixed_unsupported_collection_warns_and_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        class M(BaseModel):
            n: int

        with caplog.at_level(logging.WARNING, logger="ai_pipeline_core.prompt_contract.render"):
            _render_input_value([M(n=1), "string"])
        assert any("unsupported item types" in record.message for record in caplog.records)
