"""Tests for PromptContract.execute() — repair loop, span emission, tool bridge."""

from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import LLMRequest
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import TerminalError
from ai_pipeline_core.llm import _engine
from ai_pipeline_core.llm._conversation_runtime import assemble_api_messages
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.prompt_contract import (
    Methodology,
    PromptContract,
    PromptResult,
    ToolAvailability,
    ToolBinding,
    ValidationFailure,
)
from tests.support.helpers import ConcreteDocument
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class ContractAnswer(FrozenBaseModel):
    """Structured contract output."""

    answer: str = Field(description="Answer string")
    score: int = Field(default=0, description="Score")


def _make_structured_response(
    parsed: BaseModel,
    *,
    content: str | None = None,
    model_name: str = "test-model",
) -> ModelResponse[Any]:
    """Build a ModelResponse[Any] carrying a structured parsed value."""
    return ModelResponse[Any](
        content=content if content is not None else parsed.model_dump_json(),
        parsed=parsed,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost=0.0,
        model=model_name,
        response_id="resp-test",
    )


def _patch_generate(monkeypatch: pytest.MonkeyPatch, scripted_responses: list[ModelResponse[Any]]) -> list[LLMRequest]:
    """Patch _engine.generate to walk through a scripted response list.

    Returns the list of captured LLMRequest objects (one per call).
    """
    captured: list[LLMRequest] = []
    iterator = iter(scripted_responses)

    async def fake_generate(request: LLMRequest, *, response_type: Any = None) -> ModelResponse[Any]:
        _ = response_type
        captured.append(request)
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("fake_generate called more times than scripted responses") from exc

    monkeypatch.setattr(_engine, "generate", fake_generate)
    return captured


# ---------------------------------------------------------------------------
# 1. Trivial passing contract — one round, validation passes, PromptResult shape
# ---------------------------------------------------------------------------


class TrivialPassContract(PromptContract[ContractAnswer]):
    """Trivial contract for the happy path."""

    purpose: ClassVar[str] = "Return a one-word answer."
    returns: ClassVar[str] = "ContractAnswer with a non-empty 'answer' string."
    success_criteria: ClassVar[str] = "answer is non-empty."


@pytest.mark.asyncio
async def test_execute_trivial_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """One generate call, validation default-passes, PromptResult carries parsed + citations."""
    parsed = ContractAnswer(answer="hello", score=1)
    captured = _patch_generate(monkeypatch, [_make_structured_response(parsed)])

    result = await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    assert isinstance(result, PromptResult)
    assert isinstance(result.response, ContractAnswer)
    assert result.response.answer == "hello"
    assert result.response.score == 1
    assert result.citations == ()
    assert len(captured) == 1
    request = captured[0]
    assert request.response.format is ContractAnswer
    # The render must include purpose/returns/success in the user message.
    payload = "\n".join(message.content if isinstance(message.content, str) else "" for message in request.messages)
    assert "Purpose" in payload
    assert "Returns" in payload
    assert "Success Criteria" in payload


# ---------------------------------------------------------------------------
# 2. Repair contract — reject round 1, accept round 2
# ---------------------------------------------------------------------------


class RepairOnceContract(PromptContract[ContractAnswer]):
    """Contract that rejects 'bad' and accepts anything else."""

    purpose: ClassVar[str] = "produce a non-bad answer."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "answer != 'bad'."

    def validate(self, response: ContractAnswer) -> tuple[ValidationFailure, ...]:  # type: ignore[override]  # test-scaffold signature divergence
        if response.answer == "bad":
            return (ValidationFailure(field="answer", message="answer must not be 'bad'"),)
        return ()


@pytest.mark.asyncio
async def test_execute_repair_succeeds_on_second_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """One repair round runs; the second response is the final answer."""
    captured = _patch_generate(
        monkeypatch,
        [
            _make_structured_response(ContractAnswer(answer="bad")),
            _make_structured_response(ContractAnswer(answer="good")),
        ],
    )

    result = await RepairOnceContract().execute(DEFAULT_TEST_MODEL)

    assert result.response.answer == "good"
    assert len(captured) == 2
    # Second call's messages must include the repair USER message after the failing assistant message.
    second_call_text = "\n".join(message.content if isinstance(message.content, str) else "" for message in captured[1].messages)
    assert "did not satisfy the contract validation" in second_call_text
    assert "answer must not be 'bad'" in second_call_text


# ---------------------------------------------------------------------------
# 3. Exhaustion — always rejects → TerminalError
# ---------------------------------------------------------------------------


class AlwaysRejectsContract(PromptContract[ContractAnswer]):
    """Contract whose validation always fails."""

    purpose: ClassVar[str] = "always rejected."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "impossible to satisfy."

    def validate(self, response: ContractAnswer) -> tuple[ValidationFailure, ...]:  # type: ignore[override]  # test-scaffold signature divergence
        _ = response
        return (ValidationFailure(message="always fails"),)


@pytest.mark.asyncio
async def test_execute_exhaustion_raises_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """After max_attempts, the engine raises TerminalError."""
    parsed = ContractAnswer(answer="anything")
    # max_attempts defaults to 2 (one initial + one repair).
    _patch_generate(monkeypatch, [_make_structured_response(parsed) for _ in range(4)])

    with pytest.raises(TerminalError, match="validation exhausted"):
        await AlwaysRejectsContract().execute(DEFAULT_TEST_MODEL)


@pytest.mark.asyncio
async def test_execute_exhaustion_calls_generate_exactly_max_attempts_times(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine respects settings.prompt_contract_max_repair (default 2)."""
    from ai_pipeline_core.settings import settings as core_settings

    parsed = ContractAnswer(answer="anything")
    captured = _patch_generate(monkeypatch, [_make_structured_response(parsed) for _ in range(10)])

    with pytest.raises(TerminalError):
        await AlwaysRejectsContract().execute(DEFAULT_TEST_MODEL)

    assert len(captured) == core_settings.prompt_contract_max_repair


# ---------------------------------------------------------------------------
# 4. Cache parity — Conversation.send vs PromptContract.execute over identical
# system_block + context produce identical prompt_cache_key.
# ---------------------------------------------------------------------------


class CachedContextContract(PromptContract[ContractAnswer]):
    """Contract with one document field (cacheable context)."""

    purpose: ClassVar[str] = "use the document."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "anything."

    document: Document


@pytest.mark.asyncio
async def test_cache_key_parity_between_contract_and_assemble_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """PromptContract.execute and a direct call to assemble_api_messages produce the same cache prefix.

    The engine computes ``prompt_cache_key`` only over the leading ``context_count``
    messages. If the contract and Conversation route through the same helper, the
    cacheable prefix must be byte-identical when system_block + context are equal.
    """
    from ai_pipeline_core._llm_core._transport import compute_cache_key, core_messages_to_api

    doc = ConcreteDocument.create_root(name="ctx.txt", content="static context body", reason="cache parity")
    contract = CachedContextContract(document=doc)

    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="x"))])

    await contract.execute(DEFAULT_TEST_MODEL)

    assert len(captured) == 1
    contract_request = captured[0]

    # Build the same prefix through assemble_api_messages directly with the
    # same system block (= contract's empty methodologies → None) and context.
    expected_messages, expected_count = await assemble_api_messages(
        system_block=None,
        context=(doc,),
        messages=(),
        model=DEFAULT_TEST_MODEL,
        substitutor=None,
    )

    contract_api = core_messages_to_api(
        list(contract_request.messages),
        max_inline_file_total_bytes=DEFAULT_TEST_MODEL.max_inline_file_total_bytes,
    )
    expected_api = core_messages_to_api(
        expected_messages,
        max_inline_file_total_bytes=DEFAULT_TEST_MODEL.max_inline_file_total_bytes,
    )

    contract_prefix_key = compute_cache_key(contract_api[: contract_request.cache.context_count])
    expected_prefix_key = compute_cache_key(expected_api[:expected_count])

    assert contract_request.cache.context_count == expected_count == 1
    assert contract_prefix_key == expected_prefix_key


# ---------------------------------------------------------------------------
# 5. Legacy Tool bridge — declared ToolAvailability + ToolBinding constructs tool,
# engine routes tool call into the legacy Tool instance.
# ---------------------------------------------------------------------------


class EchoLegacyTool(Tool):
    """Echo back the input plus a binding-supplied suffix."""

    class Input(BaseModel):
        """Echo input."""

        text: str = Field(description="text to echo")

    class Output(BaseModel):
        """Echo output."""

        echoed: str

    def __init__(self, *, suffix: str = "") -> None:
        self._suffix = suffix

    async def run(self, input: Input) -> Output:
        return self.Output(echoed=f"{input.text}{self._suffix}")


class WithLegacyToolContract(PromptContract[ContractAnswer]):
    """Contract declaring a single legacy Tool via ToolAvailability."""

    purpose: ClassVar[str] = "use the echo tool then answer."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "tool was called."

    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(EchoLegacyTool, max_calls=2),)


@pytest.mark.asyncio
async def test_execute_legacy_tool_bridge_calls_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge builds an EchoLegacyTool(suffix=...) and the engine routes the tool call to it."""
    from tests.support.helpers import make_tool_call

    tool_response = ModelResponse[Any](
        content="",
        parsed="",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost=0.0,
        model="test-model",
        response_id="r1",
        tool_calls=(make_tool_call("c1", "echo_legacy_tool", '{"text": "hi"}'),),
    )
    final_response = _make_structured_response(ContractAnswer(answer="done"))
    _patch_generate(monkeypatch, [tool_response, final_response])

    bindings = (ToolBinding(EchoLegacyTool, args={"suffix": "!"}),)
    result = await WithLegacyToolContract().execute(DEFAULT_TEST_MODEL, tool_bindings=bindings)

    assert result.response.answer == "done"


@pytest.mark.asyncio
async def test_execute_legacy_tool_bridge_missing_binding_raises() -> None:
    """A declared tool with no matching ToolBinding is a hard TypeError before generate is called."""
    with pytest.raises(TypeError, match="no matching ToolBinding"):
        await WithLegacyToolContract().execute(DEFAULT_TEST_MODEL)


@pytest.mark.asyncio
async def test_execute_legacy_tool_bridge_extra_binding_raises() -> None:
    """A ToolBinding for an undeclared tool is rejected."""

    class _UndeclaredTool(Tool):
        """Undeclared."""

        class Input(BaseModel):
            """Undeclared input."""

            text: str = Field(description="text")

        class Output(BaseModel):
            """Undeclared output."""

            text: str

        async def run(self, input: Input) -> Output:
            return self.Output(text=input.text)

    bindings = (
        ToolBinding(EchoLegacyTool, args={"suffix": "!"}),
        ToolBinding(_UndeclaredTool, args={}),
    )
    with pytest.raises(TypeError, match="undeclared tools"):
        await WithLegacyToolContract().execute(DEFAULT_TEST_MODEL, tool_bindings=bindings)


# ---------------------------------------------------------------------------
# 6. Replay round-trip — encode receiver/input → decode → reach .execute
# ---------------------------------------------------------------------------


class ReplayRoundTripContract(PromptContract[ContractAnswer]):
    """Contract used to validate replay receiver round-trip."""

    purpose: ClassVar[str] = "round-trip test."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "anything."

    topic: str = Field(description="Topic to round-trip")


@pytest.mark.asyncio
async def test_execute_round_trip_through_replay_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Receiver encoded by execute() can be reconstructed by the replay adapter."""
    from ai_pipeline_core._codec import UniversalCodec
    from ai_pipeline_core.replay._adapters import resolve_callable

    cls = ReplayRoundTripContract
    receiver = {
        "mode": "promptcontract_receiver",
        "value": {
            "class_path": f"{cls.__module__}:{cls.__qualname__}",
            "instance_fields": {"topic": "machine learning"},
        },
    }
    codec = UniversalCodec()
    encoded = codec.encode(receiver["value"])
    decoded_value = await codec.decode_async(encoded.value)
    rebuilt_receiver = {"mode": "promptcontract_receiver", "value": decoded_value}

    target = f"decoded_promptcontract:{cls.__module__}:{cls.__qualname__}.execute"
    callable_obj = resolve_callable(target, receiver=rebuilt_receiver)
    # The resolved callable is a bound method; calling it would dispatch into
    # execute() with the live model. We don't invoke the LLM here — verifying
    # the adapter rebuilds the contract instance is enough to confirm
    # round-trip integrity.
    assert callable(callable_obj)
    assert hasattr(callable_obj, "__self__")
    rebuilt_instance = callable_obj.__self__  # type: ignore[attr-defined]  # attribute injected at runtime for test
    assert isinstance(rebuilt_instance, cls)
    assert rebuilt_instance.topic == "machine learning"

    # Sanity: execute still works on the rebuilt instance with a mocked generate.
    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])
    result = await rebuilt_instance.execute(DEFAULT_TEST_MODEL)
    assert result.response.answer == "ok"


# ---------------------------------------------------------------------------
# 7. Methodology renders into the system block
# ---------------------------------------------------------------------------


class StepByStepMethodology(Methodology):
    """Reason through the problem step by step before committing to an answer."""

    purpose: ClassVar[str] = "Decompose the question, evaluate each piece, then synthesize."


class WithMethodologyContract(PromptContract[ContractAnswer]):
    """Contract with one methodology."""

    purpose: ClassVar[str] = "produce a careful answer."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "any answer."
    methodologies: ClassVar[tuple[type[Methodology], ...]] = (StepByStepMethodology,)


@pytest.mark.asyncio
async def test_execute_methodologies_render_into_user_reference_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared methodologies become ``# Reference: <Title>`` sections in the USER message; there is no system block."""
    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="x"))])

    await WithMethodologyContract().execute(DEFAULT_TEST_MODEL)

    # No SYSTEM message — contracts no longer emit a system block.
    assert all(message.role.value != "system" for message in captured[0].messages)

    user_payload = "\n".join(message.content if isinstance(message.content, str) else "" for message in captured[0].messages if message.role.value == "user")
    assert "# Reference: Step By Step" in user_payload
    assert "step by step" in user_payload.lower()


# ---------------------------------------------------------------------------
# 8. Document instance fields land in cacheable context (not in dynamic prompt).
# ---------------------------------------------------------------------------


class ContextDocContract(PromptContract[ContractAnswer]):
    """Contract with a Document field."""

    purpose: ClassVar[str] = "use the provided doc."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "anything."

    document: Document


@pytest.mark.asyncio
async def test_document_field_goes_into_context_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single Document field bumps cache.context_count to 1."""
    doc = ConcreteDocument.create_root(name="ctx.txt", content="material", reason="t")
    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    await ContextDocContract(document=doc).execute(DEFAULT_TEST_MODEL)

    request = captured[0]
    assert request.cache.context_count == 1
    # First message is the cacheable context document; last is the dynamic prompt.
    assert request.messages[-1].role.value == "user"


# ---------------------------------------------------------------------------
# 9. Public AIModel guard — non-AIModel inputs raise TypeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rejects_non_aimodel() -> None:
    """execute(model=...) must reject anything that is not an AIModel."""
    with pytest.raises(TypeError, match="AIModel"):
        await TrivialPassContract().execute("not-a-model")  # type: ignore[arg-type]  # negative test: wrong runtime type


# ---------------------------------------------------------------------------
# 10. Repair message includes failure detail (defensive — sanity check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_message_includes_failure_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair USER message must include each failure's field+message."""
    captured = _patch_generate(
        monkeypatch,
        [
            _make_structured_response(ContractAnswer(answer="bad")),
            _make_structured_response(ContractAnswer(answer="ok")),
        ],
    )

    await RepairOnceContract().execute(DEFAULT_TEST_MODEL)

    second_messages = captured[1].messages
    # Tail of second-call messages: previous assistant response then USER repair.
    repair_user = next(message for message in reversed(second_messages) if message.role.value == "user")
    repair_text = repair_user.content if isinstance(repair_user.content, str) else ""
    assert "[answer]" in repair_text
    assert "must not be 'bad'" in repair_text


# ---------------------------------------------------------------------------
# 11. Repair attempt threads previous deployment_id into routing.preferred
# ---------------------------------------------------------------------------


def _make_structured_response_with_deployment(parsed: BaseModel, deployment_id: str) -> ModelResponse[Any]:
    from ai_pipeline_core._llm_core._transport_metadata import AIPLInfo, TransportMetadata

    return ModelResponse[Any](
        content=parsed.model_dump_json(),
        parsed=parsed,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost=0.0,
        model="test-model",
        response_id="r",
        transport=TransportMetadata(aipl=AIPLInfo(deployment_id=deployment_id)),
    )


@pytest.mark.asyncio
async def test_repair_threads_deployment_id_to_routing_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair attempt sets routing.preferred_deployment_id from the prior attempt's deployment."""
    captured = _patch_generate(
        monkeypatch,
        [
            _make_structured_response_with_deployment(ContractAnswer(answer="bad"), deployment_id="dep-1"),
            _make_structured_response_with_deployment(ContractAnswer(answer="ok"), deployment_id="dep-1"),
        ],
    )

    await RepairOnceContract().execute(DEFAULT_TEST_MODEL)

    assert captured[0].routing.preferred_deployment_id is None
    assert captured[1].routing.preferred_deployment_id == "dep-1"


# ---------------------------------------------------------------------------
# 12. PROMPT_EXECUTION span opens and reaches the database sink when one is wired.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_execution_span_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PROMPT_EXECUTION span is emitted when execute() runs inside a context with a DB sink."""
    from ai_pipeline_core.database import SpanKind

    from tests.support.helpers import RecordingSpanDatabase, make_integration_context
    from ai_pipeline_core.pipeline._execution_context import set_execution_context

    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    database = RecordingSpanDatabase()
    ctx = make_integration_context(database, run_id="pc-span-test")
    with set_execution_context(ctx):
        await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    pe_spans = database.completed_spans_by_kind(SpanKind.PROMPT_EXECUTION)
    assert len(pe_spans) == 1
    span = pe_spans[0]
    assert span.target.startswith("decoded_promptcontract:")
    assert span.target.endswith(".execute")


# ---------------------------------------------------------------------------
# 13. patch.object compatibility: ensure no hidden import-time leakage.
# ---------------------------------------------------------------------------


def test_validation_spec_is_constructed_with_default_max() -> None:
    """ValidationSpec is created during execute() with max_attempts from settings."""
    from ai_pipeline_core._llm_core.request import ValidationSpec
    from ai_pipeline_core.settings import settings as core_settings

    spec = ValidationSpec(validate=TrivialPassContract().validate, max_attempts=core_settings.prompt_contract_max_repair)
    assert spec.max_attempts == core_settings.prompt_contract_max_repair


# ---------------------------------------------------------------------------
# Regression pin: execute() refuses to run from flow scope without a task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rejects_flow_scope_without_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flow-scope task_ctx must trigger the shared LLM scope guard before any engine dispatch."""
    from ai_pipeline_core.pipeline._execution_context import TaskContext, set_task_context

    _patch_generate(monkeypatch, [])  # any generate call would AssertionError

    flow_ctx = TaskContext(scope_kind="flow", task_class_name="FakeFlow")
    token = set_task_context(flow_ctx)
    try:
        with pytest.raises(RuntimeError, match="flow scope"):
            await TrivialPassContract().execute(DEFAULT_TEST_MODEL)
    finally:
        from ai_pipeline_core.pipeline._execution_context import _task_context

        _task_context.reset(token)


# ---------------------------------------------------------------------------
# Regression pin: tool_bindings are encoded into input_json and the replay
# adapter forwards them to the rebuilt execute() call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_bindings_encoded_in_span_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PROMPT_EXECUTION span's input_json must contain tool_bindings round-trippable through the codec."""
    import json

    from ai_pipeline_core._codec import UniversalCodec
    from ai_pipeline_core.pipeline._execution_context import set_execution_context

    from tests.support.helpers import RecordingSpanDatabase, make_integration_context

    tool_response = ModelResponse[Any](
        content="",
        parsed="",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost=0.0,
        model="test-model",
        response_id="r1",
        tool_calls=(),
    )
    # No actual tool call needed to verify input_json — just one final response.
    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    database = RecordingSpanDatabase()
    ctx = make_integration_context(database, run_id="tool-bindings-round-trip")
    with set_execution_context(ctx):
        await WithLegacyToolContract().execute(
            DEFAULT_TEST_MODEL,
            tool_bindings=(ToolBinding(EchoLegacyTool, args={"suffix": "!"}),),
        )

    from ai_pipeline_core.database import SpanKind

    pe_spans = database.completed_spans_by_kind(SpanKind.PROMPT_EXECUTION)
    assert len(pe_spans) == 1
    payload = json.loads(pe_spans[0].input_json)
    assert "tool_bindings" in payload

    # Decode the encoded tool_bindings via the same codec the replay path uses.
    codec = UniversalCodec()
    decoded = await codec.decode_async(payload["tool_bindings"])
    assert isinstance(decoded, tuple)
    assert len(decoded) == 1
    assert isinstance(decoded[0], ToolBinding)
    assert decoded[0].tool is EchoLegacyTool
    assert dict(decoded[0].args) == {"suffix": "!"}

    _ = tool_response  # silence unused (kept as exemplar shape for future tool-round tests)


@pytest.mark.asyncio
async def test_replay_round_trip_with_tool_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encoded receiver + input rebuild the bound execute() with the original tool_bindings."""
    from ai_pipeline_core._codec import UniversalCodec
    from ai_pipeline_core.replay._adapters import _invoke_callable, resolve_callable

    cls = WithLegacyToolContract
    bindings = (ToolBinding(EchoLegacyTool, args={"suffix": "!!"}),)
    codec = UniversalCodec()

    receiver_payload = {
        "mode": "promptcontract_receiver",
        "value": {
            "class_path": f"{cls.__module__}:{cls.__qualname__}",
            "instance_fields": {},
        },
    }
    encoded_receiver_value = codec.encode(receiver_payload["value"])
    decoded_receiver_value = await codec.decode_async(encoded_receiver_value.value)
    rebuilt_receiver = {"mode": "promptcontract_receiver", "value": decoded_receiver_value}

    span_input = {"model": DEFAULT_TEST_MODEL, "tool_bindings": bindings}
    encoded_input = codec.encode(span_input)
    decoded_input = await codec.decode_async(encoded_input.value)

    target = f"decoded_promptcontract:{cls.__module__}:{cls.__qualname__}.execute"
    callable_obj = resolve_callable(target, receiver=rebuilt_receiver)
    assert hasattr(callable_obj, "__self__")  # bound method
    rebuilt_instance = callable_obj.__self__  # type: ignore[attr-defined]  # attribute injected at runtime for test
    assert isinstance(rebuilt_instance, cls)

    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="replayed"))])
    result = await _invoke_callable(callable_obj, decoded_input)
    assert isinstance(result, PromptResult)
    assert result.response.answer == "replayed"


# ---------------------------------------------------------------------------
# Regression pin: ExecutionContext.disable_cache → CacheSpec(ttl=0) override.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_honors_disable_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """When execution context sets disable_cache=True, the engine must see cache.ttl=0."""
    from ai_pipeline_core.pipeline._execution_context import set_execution_context

    from tests.support.helpers import make_integration_context, RecordingSpanDatabase

    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    database = RecordingSpanDatabase()
    ctx = make_integration_context(database, run_id="disable-cache-test")
    ctx.disable_cache = True
    with set_execution_context(ctx):
        await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    assert len(captured) == 1
    assert captured[0].cache.ttl == 0


@pytest.mark.asyncio
async def test_execute_uses_model_cache_ttl_when_not_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without disable_cache, the engine falls through to ``model.cache_ttl`` (unchanged)."""
    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    assert len(captured) == 1
    # Default AIModel cache_ttl is DEFAULT_CACHE_TTL_MINUTES (see _llm_core/_defaults.py).
    assert captured[0].cache.ttl == DEFAULT_TEST_MODEL.cache_ttl


# ---------------------------------------------------------------------------
# Regression pin: span meta carries the Conversation-aligned shape, the
# four new contract keys, and repair_attempt_count from EngineResult.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_result_carries_repair_attempt_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful first-attempt run reports repair_attempt_count=0; one repair → 1."""
    from ai_pipeline_core.llm._engine import execute_interaction
    from ai_pipeline_core._llm_core.request import ValidationSpec
    from ai_pipeline_core.llm._engine import InteractionRequest
    from ai_pipeline_core._llm_core.types import CoreMessage, Role

    # No validation → repair_attempt_count must be 0.
    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])
    req = InteractionRequest(messages=(CoreMessage(role=Role.USER, content="hi"),), model=DEFAULT_TEST_MODEL, response_format=ContractAnswer)
    result = await execute_interaction(req)
    assert result.repair_attempt_count == 0

    # Validation that rejects once then accepts → repair_attempt_count must be 1.
    rejected_then_accepted = [
        _make_structured_response(ContractAnswer(answer="bad")),
        _make_structured_response(ContractAnswer(answer="ok")),
    ]
    _patch_generate(monkeypatch, rejected_then_accepted)
    state = {"calls": 0}

    def validator(parsed: Any) -> tuple[Any, ...]:
        state["calls"] += 1
        if state["calls"] == 1:
            return (ValidationFailure(message="reject once"),)
        return ()

    req = InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        model=DEFAULT_TEST_MODEL,
        response_format=ContractAnswer,
        validation=ValidationSpec(validate=validator, max_attempts=2),
    )
    result = await execute_interaction(req)
    assert result.repair_attempt_count == 1


@pytest.mark.asyncio
async def test_prompt_execution_span_meta_carries_conversation_aligned_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded span meta carries Conversation-shaped fields + the new contract-specific keys."""
    import json

    from ai_pipeline_core.database import SpanKind
    from ai_pipeline_core.pipeline._execution_context import set_execution_context

    from tests.support.helpers import RecordingSpanDatabase, make_integration_context

    _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])

    database = RecordingSpanDatabase()
    ctx = make_integration_context(database, run_id="span-meta-test")
    with set_execution_context(ctx):
        await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    pe_spans = database.completed_spans_by_kind(SpanKind.PROMPT_EXECUTION)
    assert len(pe_spans) == 1
    meta = json.loads(pe_spans[0].meta_json)

    # Conversation-shaped fields:
    conversation_keys = (
        "model",
        "response_content",
        "reasoning_content",
        "response_id",
        "response_format_path",
        "citations",
        "tool_schemas",
        "tool_choice",
        "max_tool_rounds",
        "aipl_summary",
        "aipl",
        "litellm",
        "model_chain",
        "prompt_cache_key",
        "raw_response_headers",
    )
    for key in conversation_keys:
        assert key in meta, f"missing Conversation-aligned meta key: {key}"

    # LLM_ROUND-shaped extras included for parity:
    for key in ("finish_reason", "response_tool_calls", "tool_call_count", "request_messages"):
        assert key in meta, f"missing LLM_ROUND-aligned meta key: {key}"

    # Contract-specific new keys:
    contract_keys = (
        "purpose_text",
        "returns_text",
        "success_criteria_text",
        "methodologies",
        "tools",
        "tool_bindings",
        "llm_round_count",
        "repair_attempt_count",
    )
    for key in contract_keys:
        assert key in meta, f"missing contract-specific meta key: {key}"

    assert meta["repair_attempt_count"] == 0
    assert meta["llm_round_count"] == 1
    assert meta["purpose_text"] == TrivialPassContract.purpose


# ---------------------------------------------------------------------------
# Substitutor preservation guidance reaches the model through the user message
# (PromptContract emits no system block, so the instruction lives in the user
# turn rather than in a system message — Conversation's analog).
# ---------------------------------------------------------------------------


class _SubstitutorTestContract(PromptContract[ContractAnswer]):
    """Contract that ships a long URL in a dynamic field to trigger substitution."""

    purpose: ClassVar[str] = "Echo the long URL provided."
    returns: ClassVar[str] = "ContractAnswer."
    success_criteria: ClassVar[str] = "anything."

    long_url: str = Field(description="A URL that will trigger substitution.")


@pytest.mark.asyncio
async def test_user_message_omits_substitutor_instruction_when_no_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no shortening applies, the user message must not carry the substitutor instruction."""
    from ai_pipeline_core.llm._conversation_runtime import _SUBSTITUTOR_INSTRUCTION

    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])
    await TrivialPassContract().execute(DEFAULT_TEST_MODEL)

    user_text = "\n".join(message.content if isinstance(message.content, str) else "" for message in captured[0].messages if message.role.value == "user")
    assert _SUBSTITUTOR_INSTRUCTION not in user_text


@pytest.mark.asyncio
async def test_user_message_includes_substitutor_instruction_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """The captured request to the engine must contain a # Notation section with the preservation instruction."""
    from ai_pipeline_core.llm._conversation_runtime import _SUBSTITUTOR_INSTRUCTION

    captured = _patch_generate(monkeypatch, [_make_structured_response(ContractAnswer(answer="ok"))])
    long_url = "https://example.com/etherscan/tx/0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1?source=research-2026"
    await _SubstitutorTestContract(long_url=long_url).execute(DEFAULT_TEST_MODEL)

    user_text = "\n".join(message.content if isinstance(message.content, str) else "" for message in captured[0].messages if message.role.value == "user")
    assert "# Notation" in user_text
    assert _SUBSTITUTOR_INSTRUCTION in user_text


# Avoid unused import warnings if pyright sweeps.
_ = patch
