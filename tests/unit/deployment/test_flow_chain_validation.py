"""Tests for _validate_flow_chain type-pool validation."""

import pytest

from ai_pipeline_core.deployment.base import _validate_flow_chain
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineFlow
from ai_pipeline_core.pipeline.options import FlowOptions


class _DocA(Document):
    """Type A."""


class _DocB(Document):
    """Type B."""


class _DocC(Document):
    """Type C."""


class _DocD(Document):
    """Type D."""


class _FlowAtoB(PipelineFlow):
    async def run(self, doc_as: tuple[_DocA, ...], options: FlowOptions) -> tuple[_DocB, ...]:
        _ = (doc_as, options)
        return ()


class _FlowBtoC(PipelineFlow):
    async def run(self, doc_bs: tuple[_DocB, ...], options: FlowOptions) -> tuple[_DocC, ...]:
        _ = (doc_bs, options)
        return ()


class _FlowCtoA(PipelineFlow):
    async def run(self, doc_c: _DocC, options: FlowOptions) -> tuple[_DocA, ...]:
        _ = (doc_c, options)
        return ()


class _FlowUnionOutput(PipelineFlow):
    async def run(self, doc_as: tuple[_DocA, ...], options: FlowOptions) -> tuple[_DocA | _DocD, ...]:
        _ = (doc_as, options)
        return ()


class _FlowBaseInput(PipelineFlow):
    async def run(self, doc_a: _DocA, options: FlowOptions) -> tuple[_DocC, ...]:
        _ = (doc_a, options)
        return ()


class _RequiredFirstFlowAtoB(PipelineFlow):
    async def run(self, doc_a: _DocA, options: FlowOptions) -> tuple[_DocB, ...]:
        _ = (doc_a, options)
        return ()


class _FirstFlowOptionalInput(PipelineFlow):
    async def run(self, maybe_c: _DocC | None, options: FlowOptions) -> tuple[_DocB, ...]:
        _ = (maybe_c, options)
        return ()


class _FirstFlowCollectionInput(PipelineFlow):
    async def run(self, all_cs: tuple[_DocC, ...], options: FlowOptions) -> tuple[_DocB, ...]:
        _ = (all_cs, options)
        return ()


def test_single_flow_always_valid() -> None:
    _validate_flow_chain("test", [_FlowAtoB()])


def test_valid_chain_sequential_types() -> None:
    _validate_flow_chain("test", [_FlowAtoB(), _FlowBtoC()])


def test_invalid_chain_unsatisfied_input() -> None:
    # FlowAtoB outputs DocB, but FlowCtoA needs DocC
    with pytest.raises(TypeError, match="requires input types"):
        _validate_flow_chain("test", [_FlowAtoB(), _FlowCtoA()])


def test_type_pool_accumulates_across_flows() -> None:
    # FlowAtoB: pool = {A, B}; FlowBtoC: pool = {A, B, C}; FlowCtoA: needs C → in pool ✓
    _validate_flow_chain("test", [_FlowAtoB(), _FlowBtoC(), _FlowCtoA()])


def test_first_flow_required_singleton_inputs_added_to_pool() -> None:
    # Required singleton inputs on the first flow are guaranteed deployment inputs.
    _validate_flow_chain("test", [_RequiredFirstFlowAtoB(), _FlowBaseInput()])


def test_union_output_member_satisfies_downstream_input() -> None:
    # FlowUnionOutput may produce DocA or DocD.
    # FlowBaseInput needs DocA, so the chain remains satisfiable.
    _validate_flow_chain("test", [_FlowUnionOutput(), _FlowBaseInput()])


def test_empty_flow_list() -> None:
    _validate_flow_chain("test", [])


def test_error_message_includes_available_types() -> None:
    with pytest.raises(TypeError, match="Available types") as exc_info:
        _validate_flow_chain("test-deploy", [_FlowAtoB(), _FlowCtoA()])
    assert "test-deploy" in str(exc_info.value)
    assert "_DocC" in str(exc_info.value)


def test_first_flow_optional_input_does_not_satisfy_later_required_singleton() -> None:
    with pytest.raises(TypeError, match="requires input types"):
        _validate_flow_chain("test", [_FirstFlowOptionalInput(), _FlowCtoA()])


def test_first_flow_collection_input_does_not_satisfy_later_required_singleton() -> None:
    with pytest.raises(TypeError, match="requires input types"):
        _validate_flow_chain("test", [_FirstFlowCollectionInput(), _FlowCtoA()])
