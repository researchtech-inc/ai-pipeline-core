"""Tests for replay experiments and overrides."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from ai_pipeline_core._llm_core import LLMRequest
from ai_pipeline_core._llm_core.types import ModelOptions
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.replay import ExperimentOverrides, experiment_batch, experiment_span, find_experiment_span_ids
from tests.unit.replay.conftest import make_span
from tests.support.helpers import create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


@pytest.mark.asyncio
async def test_experiment_span_extracts_original_output_and_applies_overrides(
    monkeypatch: pytest.MonkeyPatch,
    memory_database,
) -> None:
    seen_kwargs: dict[str, Any] = {}

    async def fake_generate(request: LLMRequest) -> Any:
        seen_kwargs["model"] = request.model
        seen_kwargs["reasoning_effort"] = request.generation.reasoning_effort
        return create_test_model_response(
            content="replayed",
            model=request.model.name,
            prompt_tokens=7,
            completion_tokens=3,
            cost=0.12,
        )

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    span = make_span(
        kind="conversation",
        name="experiment",
        target="decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        receiver_mode="decoded_state",
        receiver_value=Conversation(
            model=DEFAULT_TEST_MODEL,
            model_options=ModelOptions(temperature=0.2),
        ),
        input_value={
            "content": "hello",
            "tools": (),
            "tool_choice": None,
            "max_tool_rounds": 1,
            "purpose": "analysis",
            "response_format": None,
        },
        meta={"model": "stored-model", "response_content": "stored response", "purpose": "analysis"},
        metrics={"tokens_input": 11, "tokens_output": 5, "time_taken_ms": 321, "cost_usd": 0.45},
        cost_usd=0.45,
    )
    await memory_database.insert_span(span)

    result = await experiment_span(
        span.span_id,
        source_db=memory_database,
        sink_db=memory_database,
        overrides=ExperimentOverrides(
            model=ALTERNATE_TEST_MODEL,
            model_options={"reasoning_effort": "high"},
        ),
    )

    assert result.original.model == "stored-model"
    assert result.original.response_text == "stored response"
    assert result.original.cost_usd == 0.45
    assert result.original.tokens_input == 11
    assert result.original.tokens_output == 5
    assert result.original.duration_ms == 321
    assert result.result.content == "replayed"
    assert seen_kwargs["model"] == ALTERNATE_TEST_MODEL
    assert seen_kwargs["reasoning_effort"] == "high"
    assert result.replay_run_id.startswith(f"replay-{str(span.span_id)[:8]}-")


@pytest.mark.asyncio
async def test_experiment_batch_limits_concurrency_and_preserves_results(
    monkeypatch: pytest.MonkeyPatch,
    memory_database,
) -> None:
    overrides = ExperimentOverrides(model=ALTERNATE_TEST_MODEL)
    span_ids = [uuid4(), uuid4(), uuid4()]
    active = 0
    max_active = 0

    async def fake_experiment_span(
        span_id: Any,
        *,
        source_db: Any,
        sink_db: Any = None,
        overrides: Any = None,
    ) -> Any:
        nonlocal active, max_active
        _ = (source_db, sink_db)
        assert overrides == override_values
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1
        return span_id

    override_values = overrides
    monkeypatch.setattr("ai_pipeline_core.replay._experiment.experiment_span", fake_experiment_span)
    results = await experiment_batch(
        span_ids,
        memory_database,
        overrides=override_values,
        concurrency=2,
    )

    assert results == span_ids
    assert max_active <= 2


@pytest.mark.asyncio
async def test_find_experiment_span_ids_filters_by_kind_purpose_and_task_class(memory_database) -> None:
    task_span = make_span(
        kind="task",
        name="task",
        target=f"classmethod:{__name__}:ExperimentTask.run",
        meta={},
    )
    conversation_span = make_span(
        kind="conversation",
        name="conversation",
        target="decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        meta={"purpose": "analysis"},
        deployment_id=task_span.deployment_id,
        root_deployment_id=task_span.root_deployment_id,
    )
    await memory_database.insert_span(task_span)
    await memory_database.insert_span(conversation_span)

    by_kind = await find_experiment_span_ids(memory_database, task_span.root_deployment_id, kind="conversation")
    by_purpose = await find_experiment_span_ids(memory_database, task_span.root_deployment_id, purpose="analysis")
    by_task_class = await find_experiment_span_ids(
        memory_database,
        task_span.root_deployment_id,
        task_class=f"{__name__}:ExperimentTask",
    )

    assert by_kind == [conversation_span.span_id]
    assert by_purpose == [conversation_span.span_id]
    assert by_task_class == [task_span.span_id]


class ExperimentTask:
    """Target class name used for task-class filtering."""
