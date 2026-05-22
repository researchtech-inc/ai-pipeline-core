"""Tests for primitive LLM core types."""

from typing import Literal

import pytest

from ai_pipeline_core._llm_core.types import AIModel
from tests.support.model_catalog import DEFAULT_TEST_MODEL

type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


def test_aimodel_reasoning_effort_defaults_to_medium() -> None:
    assert AIModel(name=DEFAULT_TEST_MODEL.name).reasoning_effort == "medium"


@pytest.mark.parametrize("reasoning_effort", ["none", "minimal", "low", "medium", "high", "xhigh"])
def test_aimodel_reasoning_effort_accepts_supported_values(reasoning_effort: ReasoningEffort) -> None:
    assert AIModel(name=DEFAULT_TEST_MODEL.name, reasoning_effort=reasoning_effort).reasoning_effort == reasoning_effort


def test_aimodel_cache_ttl_accepts_integer_minutes() -> None:
    """cache_ttl is integer minutes; default = 5, 0 disables markers, 1..1440 valid range."""
    assert AIModel(name=DEFAULT_TEST_MODEL.name).cache_ttl == 5
    assert AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=5).cache_ttl == 5
    assert AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=10).cache_ttl == 10
    assert AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=0).cache_ttl == 0


@pytest.mark.parametrize("cache_ttl", [-1, 1441, 9999])
def test_aimodel_cache_ttl_rejects_out_of_range(cache_ttl: int) -> None:
    """cache_ttl outside 0..1440 raises at construction."""
    with pytest.raises(ValueError, match="cache_ttl must be"):
        AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=cache_ttl)


def test_aimodel_cache_ttl_rejects_strings_and_none() -> None:
    """cache_ttl must be int — strings and None are no longer accepted."""
    with pytest.raises(TypeError, match="cache_ttl must be int"):
        AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl="30s")  # type: ignore[arg-type]  # negative test: wrong runtime type
    with pytest.raises(TypeError, match="cache_ttl must be int"):
        AIModel(name=DEFAULT_TEST_MODEL.name, cache_ttl=None)  # type: ignore[arg-type]  # negative test: wrong runtime type


def test_positional_name_constructor() -> None:
    """FlowOptions can define model defaults concisely."""
    model = AIModel(DEFAULT_TEST_MODEL.name)

    assert model.name == DEFAULT_TEST_MODEL.name
    assert model.preserve_input_urls is False


def test_preserve_input_urls_defaults_to_false() -> None:
    """preserve_input_urls is opt-in; never inferred from the model name."""
    assert AIModel(name=DEFAULT_TEST_MODEL.name).preserve_input_urls is False


def test_explicit_preserve_input_urls_is_authoritative() -> None:
    """Explicit AIModel capability values are authoritative."""
    assert AIModel(name=DEFAULT_TEST_MODEL.name, preserve_input_urls=True).preserve_input_urls is True


def test_capability_flags_default_true() -> None:
    """The four declarative capability flags default to True (permissive)."""
    model = AIModel(name=DEFAULT_TEST_MODEL.name)
    assert model.supports_structured_output is True
    assert model.supports_tools is True
    assert model.supports_images is True
    assert model.supports_pdfs is True
