"""Tests for register_contract enforcement in pipeline._file_rules."""

import pytest

from ai_pipeline_core.pipeline import _file_rules
from ai_pipeline_core.pipeline._file_rules import (
    register_contract,
    register_flow,
    register_spec,
    register_task,
    reset_registries,
)


@pytest.fixture
def fresh_registries():
    reset_registries()
    yield
    reset_registries()


def _fake_class(name: str, module: str) -> type:
    cls = type(name, (), {})
    cls.__module__ = module
    return cls


# ---------------------------------------------------------------------------
# register_contract: one per file
# ---------------------------------------------------------------------------


def test_one_contract_per_file(fresh_registries) -> None:
    register_contract(_fake_class("FirstContract", "app.contracts.example"))
    with pytest.raises(TypeError, match="at most one PromptContract"):
        register_contract(_fake_class("SecondContract", "app.contracts.example"))


def test_two_contracts_different_modules(fresh_registries) -> None:
    register_contract(_fake_class("FirstContract", "app.contracts.alpha"))
    # No raise — different module
    register_contract(_fake_class("SecondContract", "app.contracts.beta"))


# ---------------------------------------------------------------------------
# Contract + flow/task/spec: mutually exclusive in the same file
# ---------------------------------------------------------------------------


def test_contract_after_flow_in_same_file_raises(fresh_registries) -> None:
    register_flow(_fake_class("MyFlow", "app.mixed"))
    with pytest.raises(TypeError, match="already contains PipelineFlow"):
        register_contract(_fake_class("MyContract", "app.mixed"))


def test_contract_after_task_in_same_file_raises(fresh_registries) -> None:
    register_task(_fake_class("MyTask", "app.mixed_task"))
    with pytest.raises(TypeError, match="already contains PipelineTask"):
        register_contract(_fake_class("MyContract", "app.mixed_task"))


def test_contract_after_spec_in_same_file_raises(fresh_registries) -> None:
    register_spec(_fake_class("MySpec", "app.mixed_spec"), follows=None)
    with pytest.raises(TypeError, match="already contains PromptSpec"):
        register_contract(_fake_class("MyContract", "app.mixed_spec"))


def test_flow_after_contract_in_same_file_raises(fresh_registries) -> None:
    register_contract(_fake_class("MyContract", "app.mixed2"))
    with pytest.raises(TypeError, match="already contains PromptContract"):
        register_flow(_fake_class("MyFlow", "app.mixed2"))


def test_task_after_contract_in_same_file_raises(fresh_registries) -> None:
    register_contract(_fake_class("MyContract", "app.mixed3"))
    with pytest.raises(TypeError, match="already contains PromptContract"):
        register_task(_fake_class("MyTask", "app.mixed3"))


def test_spec_after_contract_in_same_file_raises(fresh_registries) -> None:
    register_contract(_fake_class("MyContract", "app.mixed4"))
    with pytest.raises(TypeError, match="already contains PromptContract"):
        register_spec(_fake_class("MySpec", "app.mixed4"), follows=None)


# ---------------------------------------------------------------------------
# reset_registries clears contract registry too
# ---------------------------------------------------------------------------


def test_reset_registries_clears_contracts() -> None:
    register_contract(_fake_class("MyContract", "app.reset"))
    assert _file_rules._contracts.get("app.reset") == "MyContract"
    reset_registries()
    assert "app.reset" not in _file_rules._contracts
