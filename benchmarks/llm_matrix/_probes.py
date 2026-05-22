"""Canonical live probes for the LLM benchmark matrix."""

import asyncio
import hashlib
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core import CacheSpec, CoreMessage, GenerationSpec, LLMRequest, ListOf, ResponseSpec, RetrySpec, Role, RoutingSpec
from ai_pipeline_core._llm_core._defaults import DEFAULT_CACHE_TTL
from ai_pipeline_core._llm_core.client import generate
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.llm import Conversation, ModelOptions, Tool
from ai_pipeline_core.llm._conversation_runtime import tool_runtime, to_core_messages
from ai_pipeline_core.llm._engine import InteractionRequest, execute_interaction
from ai_pipeline_core.pipeline._execution_context import set_execution_context
from ai_pipeline_core.replay import execute_span
from tests.support.helpers import (
    ConcreteDocument,
    RecordingSpanDatabase,
    last_model_response,
    make_integration_context,
    make_text_image_tile,
    pdf_with_marker,
    span_meta,
)

from ._models import BenchmarkCase, ProbeResult

_TIMEOUT_SECONDS = 120.0
_COST_TOLERANCE_USD = 0.0005
_IMAGE_MARKERS = ("GOLF", "HOTEL", "INDIA", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER")


class ProbePartialAssertion(AssertionError):
    """Assertion carrying a metadata-rich partial benchmark result."""

    def __init__(self, result: ProbeResult) -> None:
        note = "; ".join(result.notes)
        super().__init__(note)
        self.result = result


class BenchStructuredChild(BaseModel, frozen=True):
    """Child item for structured benchmark output."""

    label: str = Field(description="Short label.")
    count: int = Field(description="Small integer count.")


class BenchStructuredPayload(BaseModel, frozen=True):
    """Structured benchmark output with scalar and list fields."""

    subject: str = Field(description="Subject being summarized.")
    category: Literal["alpha", "beta"] = Field(description="Category.")
    is_valid: bool = Field(description="Whether the payload is valid.")
    children: list[BenchStructuredChild] = Field(description="Child rows.")


class BenchListItem(BaseModel, frozen=True):
    """One item returned by the ListOf benchmark probe."""

    name: str = Field(description="Item name.")
    rank: int = Field(description="Item rank.")


class BenchToolAnswer(BaseModel, frozen=True):
    """Structured answer produced after a benchmark tool call."""

    city: str = Field(description="City returned by the tool.")
    country: str = Field(description="Country used for lookup.")


class BenchImageWords(BaseModel, frozen=True):
    """Words read from image content."""

    words: list[str] = Field(description="Words visible in the image.")


class BenchCapitalTool(Tool):
    """Return the capital city for a small deterministic country table."""

    class Input(BaseModel):
        country: str = Field(description="Country name.")

    class Output(BaseModel):
        country: str = Field(description="Country name.")
        city: str = Field(description="Capital city.")

    async def run(self, input: Input) -> Output:
        capitals = {"france": "Paris", "japan": "Tokyo", "germany": "Berlin"}
        return self.Output(country=input.country, city=capitals.get(input.country.lower(), "Unknown"))


class BenchPopulationTool(Tool):
    """Return a deterministic population bucket for a city."""

    class Input(BaseModel):
        city: str = Field(description="City name.")

    class Output(BaseModel):
        city: str = Field(description="City name.")
        bucket: str = Field(description="Population bucket.")

    async def run(self, input: Input) -> Output:
        buckets = {"paris": "large", "tokyo": "very large", "berlin": "large"}
        return self.Output(city=input.city, bucket=buckets.get(input.city.lower(), "unknown"))


class BenchFailingTool(Tool):
    """Raise a deterministic exception so the tool loop records an error output."""

    class Input(BaseModel):
        reason: str = Field(description="Failure reason.")

    class Output(BaseModel):
        result: str = Field(description="Result.")

    async def run(self, input: Input) -> Output:
        raise RuntimeError(f"intentional benchmark failure: {input.reason}")


async def probe_basic_generation(case: BenchmarkCase) -> ProbeResult:
    """Probe basic text generation."""
    response = await _generate(case, f"Reply with exactly: BENCH OK. Nonce {case.nonce}.")
    _require_partial(response.content != "", case, response, "response content is empty.")
    _require_partial(response.usage.prompt_tokens > 0, case, response, "prompt token count is absent.")
    _require_partial(response.usage.completion_tokens >= 0, case, response, "completion token count is invalid.")
    _require_partial(response.cost is not None and response.cost >= 0, case, response, "response cost is absent.")
    return _pass(case, response)


async def probe_aipl_metadata(case: BenchmarkCase) -> ProbeResult:
    """Probe AIPL response metadata."""
    response = await _generate(case, f"Reply with metadata-ok. Nonce {case.nonce}.")
    aipl = response.transport.aipl
    _require_partial(bool(aipl.call_id), case, response, "AIPL call_id is absent.")
    _require_partial(bool(aipl.deployment_id), case, response, "AIPL deployment_id is absent.")
    _require_partial(bool(aipl.provider), case, response, "AIPL provider is absent.")
    _require_partial(aipl.group_status in {"ok", "degraded", "exhausted"}, case, response, "AIPL group_status is absent or invalid.")
    if case.deployment_id is not None:
        assert aipl.deployment_id == case.deployment_id
    return _pass(case, response)


async def probe_cost_sync(case: BenchmarkCase) -> ProbeResult:
    """Probe response cost and LiteLLM cost header agreement."""
    response = await _generate(case, f"Reply with cost-ok. Nonce {case.nonce}.")
    if response.cost is None:
        _raise_partial(case, response, "response cost is absent.")
    header_cost = response.transport.litellm.response_cost
    if header_cost is None:
        _raise_partial(case, response, "LiteLLM response cost header is absent.")
    if abs(float(response.cost) - float(header_cost)) > _COST_TOLERANCE_USD:
        _raise_partial(case, response, f"response.cost={response.cost} differs from LiteLLM header={header_cost}.")
    return _pass(case, response)


async def probe_routing_telemetry(case: BenchmarkCase) -> ProbeResult:
    """Probe routing telemetry on a forced deployment request."""
    response = await _generate(case, f"Reply with routing-ok. Nonce {case.nonce}.")
    _require_partial(response.transport.model_chain.requested == case.model.name, case, response, "requested model telemetry does not match the case model.")
    _require_partial(response.transport.model_chain.active == case.model.name, case, response, "active model telemetry does not match the case model.")
    if case.deployment_id is not None:
        assert response.transport.aipl.deployment_id == case.deployment_id
    return _pass(case, response)


async def probe_structured_basemodel(case: BenchmarkCase) -> ProbeResult:
    """Probe BaseModel structured output."""
    response = await _generate(
        case,
        "Return a JSON object matching the schema: subject='benchmark', category='alpha', is_valid=true, and two children with counts 1 and 2.",
        response_format=BenchStructuredPayload,
    )
    parsed = response.parsed
    assert isinstance(parsed, BenchStructuredPayload)
    assert parsed.subject.lower() == "benchmark"
    assert parsed.category == "alpha"
    assert parsed.is_valid is True
    assert len(parsed.children) == 2
    if case.expected == "partial":
        _raise_partial(case, response, "Deployment disables strict JSON schema; parsed output is accepted without strict provider enforcement.")
    return _pass(case, response)


async def probe_structured_listof(case: BenchmarkCase) -> ProbeResult:
    """Probe ListOf structured output unwrapping."""
    response = await _generate(
        case,
        "Return a JSON array matching the schema with two ranked items: alpha rank 1 and beta rank 2.",
        response_format=ListOf(BenchListItem),
    )
    parsed = response.parsed
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert all(isinstance(item, BenchListItem) for item in parsed)
    assert response.content.strip().startswith("[")
    if case.expected == "partial":
        _raise_partial(case, response, "At least one deployment for this model disables strict JSON schema; list output parsed without strict guarantee.")
    return _pass(case, response)


async def probe_tool_single(case: BenchmarkCase) -> ProbeResult:
    """Probe a single deterministic tool call."""
    result = await _interact(
        case,
        f"Use bench_capital_tool for France. Final answer must include Paris. Nonce {case.nonce}.",
        tools=[BenchCapitalTool()],
        tool_choice="required",
        max_tool_rounds=2,
    )
    assert result.tool_call_records
    assert result.tool_call_records[0].tool is BenchCapitalTool
    assert "PARIS" in result.response.content.upper()
    return _pass(case, result.response, notes=(f"tool_calls={len(result.tool_call_records)}",))


async def probe_tool_multi_with_exception(case: BenchmarkCase) -> ProbeResult:
    """Probe multiple tools including a deterministic execution exception."""
    result = await _interact(
        case,
        f"Call bench_capital_tool for Japan and bench_failing_tool with reason '{case.nonce}'. Summarize both outcomes.",
        tools=[BenchCapitalTool(), BenchFailingTool()],
        tool_choice="required",
        max_tool_rounds=3,
    )
    tool_types = {record.tool for record in result.tool_call_records}
    assert BenchCapitalTool in tool_types
    assert BenchFailingTool in tool_types
    failing_records = [record for record in result.tool_call_records if record.tool is BenchFailingTool]
    assert failing_records and "Error: Tool" in failing_records[0].output.content
    return _pass(case, result.response, notes=(f"tool_calls={len(result.tool_call_records)}",))


async def probe_tool_plus_structured(case: BenchmarkCase) -> ProbeResult:
    """Probe tool use followed by structured final output."""
    result = await _interact(
        case,
        f"Use bench_capital_tool for Germany, then return structured output with city and country. Nonce {case.nonce}.",
        tools=[BenchCapitalTool()],
        tool_choice="auto",
        response_format=BenchToolAnswer,
        max_tool_rounds=2,
    )
    assert result.tool_call_records, "tool was not called"
    parsed = result.response.parsed
    assert isinstance(parsed, BenchToolAnswer)
    assert parsed.city.lower() == "berlin"
    assert parsed.country.lower() == "germany"
    if case.expected == "partial":
        _raise_partial(case, result.response, "Model has at least one non-strict JSON deployment; tool+structured parsed without strict guarantee.")
    return _pass(case, result.response, notes=(f"tool_calls={len(result.tool_call_records)}",))


async def probe_image_attach_and_split(case: BenchmarkCase) -> ProbeResult:
    """Probe image attachment handling and tall-image splitting."""
    image = make_text_image_tile(list(_IMAGE_MARKERS))
    doc = ConcreteDocument.create_root(name=f"bench-{case.nonce}.jpg", content=image, reason="benchmark image probe")
    messages = tuple(to_core_messages((doc,), case.model))
    response = await _generate(
        case,
        "List all words visible in this image. The image may be split into sequential overlapping parts.",
        prefix_messages=messages,
        response_format=BenchImageWords,
    )
    parsed = response.parsed
    assert isinstance(parsed, BenchImageWords)
    words = {word.upper() for word in parsed.words}
    found = sum(1 for marker in _IMAGE_MARKERS if marker in words)
    assert found >= 6, f"Expected at least 6 of 8 split-image markers, got {found}: {sorted(words)}"
    return _pass(case, response, notes=(f"markers_found={found}",))


async def probe_pdf_attach(case: BenchmarkCase) -> ProbeResult:
    """Probe PDF attachment handling."""
    marker = f"PDFMARK{case.nonce[:6].upper()}"
    pdf_doc = ConcreteDocument.create_root(name=f"bench-{case.nonce}.pdf", content=pdf_with_marker(marker), reason="benchmark pdf probe")
    response = await _generate(
        case,
        f"What marker string appears in the PDF? Reply with {marker}.",
        prefix_messages=tuple(to_core_messages((pdf_doc,), case.model)),
    )
    assert marker in response.content.upper()
    return _pass(case, response)


async def probe_search_citations(case: BenchmarkCase) -> ProbeResult:
    """Probe search citations and LLM_ROUND span citation metadata."""
    database = RecordingSpanDatabase()
    context = make_integration_context(database, run_id=f"bench-search-{case.nonce}")
    with set_execution_context(context):
        response = await _generate(case, "Who won the Grammy Award for Album of the Year in 2026? Include citations.")
    assert response.citations
    assert any(citation.url for citation in response.citations)
    spans = database.completed_spans_by_kind(SpanKind.LLM_ROUND)
    assert spans
    citations = span_meta(spans[-1]).get("citations")
    assert isinstance(citations, list) and citations
    return _pass(case, response, notes=(f"citations={len(response.citations)}",))


async def probe_cache_plumbing(case: BenchmarkCase) -> ProbeResult:
    """Probe deterministic prompt cache key plumbing."""
    first = await _generate(case, f"Reply cache-one. Nonce {case.nonce}.")
    second = await _generate(case, f"Reply cache-two. Nonce {case.nonce}.")
    expected_key = _cache_key(case)
    assert first.transport.prompt_cache_key == expected_key
    assert second.transport.prompt_cache_key == expected_key
    assert first.transport.aipl.call_id != second.transport.aipl.call_id
    return _pass(case, second, notes=(f"first_cost={first.cost}",))


async def probe_reasoning(case: BenchmarkCase) -> ProbeResult:
    """Probe reasoning_effort acceptance and reasoning usage fields."""
    reasoning_case = case.model_copy(update={"model": case.model.model_copy(update={"reasoning_effort": "low"})})
    response = await _generate(reasoning_case, f"Compute 17 + 25 and reply with the answer only. Nonce {case.nonce}.")
    assert "42" in response.content
    assert response.usage.reasoning_tokens >= 0
    notes = (f"reasoning_content_chars={len(response.reasoning_content)}",)
    return _pass(case, response, notes=notes)


async def probe_replay_round_trip(case: BenchmarkCase) -> ProbeResult:
    """Probe LLM_ROUND replay pinning to the recorded deployment."""
    database = RecordingSpanDatabase()
    context = make_integration_context(database, run_id=f"bench-replay-{case.nonce}")
    with set_execution_context(context):
        conversation = await Conversation(
            model=case.model.model_copy(update={"skip_cost_optimized": True}),
            model_options=ModelOptions(reasoning_effort="none", retries=0, retry_delay_seconds=0, timeout=120.0),
            enable_substitutor=False,
        ).send(f"Reply replay-ok. Nonce {case.nonce}.", purpose=f"benchmark-{case.probe_key}")
    source_response = last_model_response(conversation)
    source_spans = database.completed_spans_by_kind(SpanKind.LLM_ROUND)
    assert source_spans, "no LLM_ROUND span recorded"
    source_span = source_spans[0]
    source_deployment_id = source_response.transport.aipl.deployment_id
    assert source_deployment_id
    replayed = await execute_span(source_span.span_id, source_db=database, sink_db=RecordingSpanDatabase())
    assert isinstance(replayed, ModelResponse)
    assert replayed.transport.aipl.deployment_id == source_deployment_id
    total_cost = float(source_response.cost or 0.0) + float(replayed.cost or 0.0)
    result = _pass(case, replayed, notes=(f"source_deployment={source_deployment_id}",))
    return result.model_copy(update={"cost_usd": total_cost})


async def probe_concurrency_smoke(case: BenchmarkCase) -> ProbeResult:
    """Probe concurrent sends on one cheap model."""
    responses = await asyncio.gather(
        _generate(case, f"Reply with CONCURRENCY A. Nonce {case.nonce}."),
        _generate(case, f"Reply with CONCURRENCY B. Nonce {case.nonce}."),
        _generate(case, f"Reply with CONCURRENCY C. Nonce {case.nonce}."),
    )
    call_ids = [response.transport.aipl.call_id for response in responses]
    assert len(set(call_ids)) == 3
    total_cost = sum(float(response.cost or 0.0) for response in responses)
    result = _pass(case, responses[-1], notes=(f"call_ids={','.join(str(call_id) for call_id in call_ids)}",))
    return result.model_copy(update={"cost_usd": total_cost})


PROBES = {
    "basic_generation": probe_basic_generation,
    "aipl_metadata": probe_aipl_metadata,
    "cost_sync": probe_cost_sync,
    "routing_telemetry": probe_routing_telemetry,
    "structured_basemodel": probe_structured_basemodel,
    "structured_listof": probe_structured_listof,
    "tool_single": probe_tool_single,
    "tool_multi_with_exception": probe_tool_multi_with_exception,
    "tool_plus_structured": probe_tool_plus_structured,
    "image_attach_and_split": probe_image_attach_and_split,
    "pdf_attach": probe_pdf_attach,
    "search_citations": probe_search_citations,
    "cache_plumbing": probe_cache_plumbing,
    "reasoning": probe_reasoning,
    "replay_round_trip": probe_replay_round_trip,
    "concurrency_smoke": probe_concurrency_smoke,
}


async def _generate(
    case: BenchmarkCase,
    prompt: str,
    *,
    prefix_messages: tuple[CoreMessage, ...] = (),
    response_format: type[BaseModel] | ListOf | None = None,
) -> ModelResponse[Any]:
    request = LLMRequest(
        model=case.model,
        messages=(*prefix_messages, CoreMessage(role=Role.USER, content=prompt)),
        generation=GenerationSpec(reasoning_effort=case.model.reasoning_effort),
        cache=_cache_spec(case),
        routing=_routing_spec(case),
        response=ResponseSpec(format=response_format),
        retry=RetrySpec(retries=0, retry_delay_seconds=0, timeout_s=_TIMEOUT_SECONDS),
        purpose=f"benchmark-{case.probe_key}",
    )
    return await generate(request)


async def _interact(
    case: BenchmarkCase,
    prompt: str,
    *,
    tools: list[Tool],
    tool_choice: Literal["auto", "required", "none"] | None,
    response_format: type[BaseModel] | ListOf | None = None,
    max_tool_rounds: int,
) -> Any:
    request = InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content=prompt),),
        model=case.model,
        response_format=response_format,
        tools=tool_runtime(tools, tool_choice, max_tool_rounds),
        purpose=f"benchmark-{case.probe_key}",
        generation_overrides=GenerationSpec(reasoning_effort="none"),
        cache_overrides=_cache_spec(case),
        routing_overrides=_routing_spec(case),
        retry_overrides=RetrySpec(retries=0, retry_delay_seconds=0, timeout_s=_TIMEOUT_SECONDS),
    )
    return await execute_interaction(request)


def _cache_spec(case: BenchmarkCase) -> CacheSpec:
    return CacheSpec(
        key_override=_cache_key(case),
        ttl=DEFAULT_CACHE_TTL,
        bypass_response_cache=True,
    )


def _cache_key(case: BenchmarkCase) -> str:
    deployment_or_model = case.deployment_id or case.model_name
    return hashlib.sha256(f"bench:{case.probe_key}:{deployment_or_model}:{case.nonce}".encode()).hexdigest()


def _routing_spec(case: BenchmarkCase) -> RoutingSpec:
    return RoutingSpec(force_deployment_id=case.deployment_id, workload_id=f"benchmark-{case.probe_key}")


def _pass(case: BenchmarkCase, response: ModelResponse[Any], *, notes: tuple[str, ...] = ()) -> ProbeResult:
    return ProbeResult(
        case_id=case.case_id,
        probe_key=case.probe_key,
        scope=case.scope,
        model_name=case.model_name,
        deployment_id=case.deployment_id,
        expected=case.expected,
        actual="pass",
        elapsed_ms=0,
        cost_usd=response.cost,
        aipl_call_id=response.transport.aipl.call_id,
        aipl_deployment_id=response.transport.aipl.deployment_id,
        provider=response.transport.aipl.provider,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cached_tokens=response.usage.cached_tokens,
        reasoning_tokens=response.usage.reasoning_tokens,
        notes=notes,
    )


def _require_partial(condition: bool, case: BenchmarkCase, response: ModelResponse[Any], note: str) -> None:
    if not condition:
        _raise_partial(case, response, note)


def _raise_partial(case: BenchmarkCase, response: ModelResponse[Any], note: str) -> NoReturn:
    result = _pass(case, response, notes=(note,)).model_copy(update={"actual": "partial"})
    raise ProbePartialAssertion(result)
