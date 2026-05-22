"""Tests for replay adapter resolution and invocation."""

from typing import Any

import pytest

from ai_pipeline_core._llm_core import LLMRequest
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.replay._adapters import _invoke_callable, resolve_callable
from tests.support.helpers import create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


async def adapter_function(*, value: str) -> str:
    """Return a simple function result."""
    return f"function:{value}"


class AdapterClassMethod:
    """Classmethod target used by replay adapter tests."""

    @classmethod
    async def run(cls, value: str) -> str:
        return f"{cls.__name__}:{value}"


class AdapterInstanceMethod:
    """Instance method target used by replay adapter tests."""

    def __init__(self, prefix: str, ignored: str = "") -> None:
        self.prefix = prefix
        self.ignored = ignored

    async def run(self, value: str, extra: str = "") -> str:
        return f"{self.prefix}:{value}:{extra}"


@pytest.mark.asyncio
async def test_function_adapter_resolves_and_invokes() -> None:
    callable_obj = resolve_callable(
        f"function:{__name__}:adapter_function",
        receiver=None,
    )
    result = await _invoke_callable(callable_obj, {"value": "ok"})

    assert result == "function:ok"


@pytest.mark.asyncio
async def test_classmethod_adapter_resolves_and_invokes() -> None:
    callable_obj = resolve_callable(
        f"classmethod:{__name__}:AdapterClassMethod.run",
        receiver=None,
    )
    result = await _invoke_callable(callable_obj, {"value": "ok"})

    assert result == "AdapterClassMethod:ok"


@pytest.mark.asyncio
async def test_instance_method_adapter_constructs_receiver_and_filters_kwargs() -> None:
    callable_obj = resolve_callable(
        f"instance_method:{__name__}:AdapterInstanceMethod.run",
        receiver={"mode": "constructor_args", "value": {"prefix": "instance", "ignored": "ignored"}},
    )
    result = await _invoke_callable(callable_obj, {"value": "ok", "extra": "x", "round_index": 2})

    assert result == "instance:ok:x"


@pytest.mark.asyncio
async def test_decoded_method_adapter_splits_method_name_from_class_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(request: LLMRequest) -> Any:
        _ = request
        return create_test_model_response(content="adapter response")

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    conversation = Conversation(model=DEFAULT_TEST_MODEL)
    callable_obj = resolve_callable(
        "decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        receiver={"mode": "decoded_state", "value": conversation},
    )
    result = await _invoke_callable(
        callable_obj,
        {
            "content": "hello",
            "tools": None,
            "tool_choice": None,
            "max_tool_rounds": 1,
            "purpose": "adapter",
            "response_format": None,
        },
    )

    assert result.content == "adapter response"


def test_resolve_callable_rejects_unknown_target_kind() -> None:
    with pytest.raises(ValueError, match="is not supported"):
        resolve_callable(f"unknown:{__name__}:adapter_function", receiver=None)


def test_resolve_callable_rejects_tool_call_target() -> None:
    """resolve_callable raises ValueError for tool_call:... targets."""
    with pytest.raises(ValueError, match="tool_call span"):
        resolve_callable("tool_call:some.module:SomeTool", receiver=None)


# ---------------------------------------------------------------------------
# promptcontract_receiver round-trip (PR 2 plumbing for PromptContract.execute)
# ---------------------------------------------------------------------------


class FakePromptContract:
    """Stand-in for the PromptContract base shipped in PR 3.

    Captures the receiver payload shape PR 4's ``PromptContract.execute`` will
    emit: ``class_path`` + ``instance_fields`` round-trip through the receiver,
    ``model`` arrives via the input arguments.
    """

    def __init__(self, *, label: str, weight: int = 1) -> None:
        self.label = label
        self.weight = weight

    async def execute(self, *, model: str) -> dict[str, object]:
        return {"label": self.label, "weight": self.weight, "model": model}


@pytest.mark.asyncio
async def test_promptcontract_receiver_round_trips_class_and_fields() -> None:
    """Receiver reconstructs the contract from class_path + instance_fields and dispatches .execute."""
    receiver = {
        "mode": "promptcontract_receiver",
        "value": {
            "class_path": f"{__name__}:FakePromptContract",
            "instance_fields": {"label": "alpha", "weight": 3},
        },
    }
    callable_obj = resolve_callable(
        f"decoded_promptcontract:{__name__}:FakePromptContract.execute",
        receiver=receiver,
    )
    result = await _invoke_callable(callable_obj, {"model": "test-model"})

    assert result == {"label": "alpha", "weight": 3, "model": "test-model"}


def test_promptcontract_receiver_rejects_wrong_mode() -> None:
    with pytest.raises(ValueError, match="promptcontract_receiver"):
        resolve_callable(
            f"decoded_promptcontract:{__name__}:FakePromptContract.execute",
            receiver={"mode": "decoded_state", "value": {"instance_fields": {"label": "x"}}},
        )


def test_promptcontract_receiver_rejects_non_dict_value() -> None:
    with pytest.raises(TypeError, match="class_path"):
        resolve_callable(
            f"decoded_promptcontract:{__name__}:FakePromptContract.execute",
            receiver={"mode": "promptcontract_receiver", "value": "not-a-dict"},
        )


def test_promptcontract_receiver_rejects_non_dict_instance_fields() -> None:
    with pytest.raises(TypeError, match="instance_fields"):
        resolve_callable(
            f"decoded_promptcontract:{__name__}:FakePromptContract.execute",
            receiver={"mode": "promptcontract_receiver", "value": {"instance_fields": [1, 2, 3]}},
        )


def test_promptcontract_receiver_filters_unknown_constructor_fields() -> None:
    """Forward-compat: extra keys persisted on older snapshots are dropped by _filter_kwargs."""
    receiver = {
        "mode": "promptcontract_receiver",
        "value": {
            "class_path": f"{__name__}:FakePromptContract",
            "instance_fields": {"label": "beta", "weight": 5, "stale_field": "ignored"},
        },
    }
    callable_obj = resolve_callable(
        f"decoded_promptcontract:{__name__}:FakePromptContract.execute",
        receiver=receiver,
    )
    # No TypeError raised by FakePromptContract.__init__ — extra kwarg was filtered.
    assert callable(callable_obj)
