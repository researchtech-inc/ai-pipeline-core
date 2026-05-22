"""Benchmark expectation matrix."""

from ai_pipeline_core.llm import AIModel

from ._models import BENCHMARK_MODEL_SET, BenchmarkCase, BenchmarkDeployment, BenchmarkInventory, ProbeKey, ProbeStatus

PER_DEPLOYMENT_PROBES: tuple[ProbeKey, ...] = (
    "basic_generation",
    "aipl_metadata",
    "cost_sync",
    "routing_telemetry",
    "structured_basemodel",
)
PER_MODEL_PROBES: tuple[ProbeKey, ...] = (
    "structured_listof",
    "tool_single",
    "tool_multi_with_exception",
    "tool_plus_structured",
    "image_attach_and_split",
    "pdf_attach",
    "search_citations",
    "cache_plumbing",
    "reasoning",
    "replay_round_trip",
)
GLOBAL_PROBES: tuple[ProbeKey, ...] = ("concurrency_smoke",)
CANONICAL_PROBES: tuple[ProbeKey, ...] = PER_DEPLOYMENT_PROBES + PER_MODEL_PROBES + GLOBAL_PROBES

SEARCH_MODELS = frozenset[str]()
VISION_MODELS = frozenset(BENCHMARK_MODEL_SET)
SMALL_TIER_MODELS = frozenset[str]()
DEPLOYMENT_SKIP_IDS = frozenset[str]()
STRUCTURED_PARTIAL_MODELS = frozenset[str]()
STRUCTURED_LISTOF_PARTIAL_MODELS = frozenset[str]()
STRUCTURED_PARTIAL_DEPLOYMENTS = frozenset[str]()
TOOL_SKIP_MODELS = frozenset[str]()
TOOL_PARTIAL_DEPLOYMENTS = frozenset[str]()
TOOL_PLUS_STRUCTURED_PARTIAL_MODELS = frozenset[str]()
PDF_RESPONSES_API_BROKEN_MODELS = frozenset[str]()
REPLAY_RESPONSES_API_BROKEN_MODELS = frozenset[str]()
COST_PARTIAL_MODELS = frozenset[str]()
CACHE_PARTIAL_MODELS = SEARCH_MODELS
REASONING_SKIP_MODELS = SMALL_TIER_MODELS
CONCURRENCY_SMOKE_MODEL = BENCHMARK_MODEL_SET[0]


def build_benchmark_cases(inventory: BenchmarkInventory, *, nonce: str) -> tuple[BenchmarkCase, ...]:
    """Build benchmark cases from inventory and the baseline matrix."""
    cases: list[BenchmarkCase] = []
    for deployment in inventory.concrete_deployments:
        cases.extend(_deployment_cases(deployment, nonce=nonce))
    deployments_by_model = _deployments_by_model(inventory)
    for model_name in inventory.model_names:
        model_deployments = deployments_by_model[model_name]
        cases.extend(_model_cases(model_name, model_deployments, nonce=nonce))
    cases.append(
        BenchmarkCase(
            case_id=f"global:{CONCURRENCY_SMOKE_MODEL}:concurrency_smoke",
            probe_key="concurrency_smoke",
            scope="global",
            model=AIModel(name=CONCURRENCY_SMOKE_MODEL, reasoning_effort="none"),
            expected="pass",
            nonce=nonce,
        )
    )
    return tuple(cases)


def _deployment_cases(deployment: BenchmarkDeployment, *, nonce: str) -> tuple[BenchmarkCase, ...]:
    return tuple(
        BenchmarkCase(
            case_id=f"deployment:{deployment.deployment_id}:{probe_key}",
            probe_key=probe_key,
            scope="deployment",
            model=AIModel(name=deployment.model_name, reasoning_effort="none"),
            deployment=deployment,
            expected=_deployment_expected(deployment, probe_key),
            nonce=nonce,
        )
        for probe_key in PER_DEPLOYMENT_PROBES
    )


def _model_cases(model_name: str, deployments: tuple[BenchmarkDeployment, ...], *, nonce: str) -> tuple[BenchmarkCase, ...]:
    return tuple(
        BenchmarkCase(
            case_id=f"model:{model_name}:{probe_key}",
            probe_key=probe_key,
            scope="model",
            model=AIModel(name=model_name, reasoning_effort="none"),
            expected=_model_expected(model_name, deployments, probe_key),
            nonce=nonce,
        )
        for probe_key in PER_MODEL_PROBES
    )


def _deployment_expected(deployment: BenchmarkDeployment, probe_key: ProbeKey) -> ProbeStatus:
    if deployment.deployment_id in DEPLOYMENT_SKIP_IDS:
        return "skip"
    if probe_key == "structured_basemodel" and not deployment.supports_json_schema:
        return "partial"
    if probe_key == "structured_basemodel" and deployment.model_name in STRUCTURED_PARTIAL_MODELS:
        return "partial"
    if probe_key == "structured_basemodel" and deployment.model_name in SEARCH_MODELS:
        return "partial"
    if probe_key == "structured_basemodel" and deployment.deployment_id in STRUCTURED_PARTIAL_DEPLOYMENTS:
        return "partial"
    if probe_key == "cost_sync" and deployment.model_name in COST_PARTIAL_MODELS:
        return "partial"
    return "pass"


def _model_expected(model_name: str, deployments: tuple[BenchmarkDeployment, ...], probe_key: ProbeKey) -> ProbeStatus:
    schema_partial = model_name in STRUCTURED_PARTIAL_MODELS or any(not deployment.supports_json_schema for deployment in deployments)
    if probe_key == "structured_listof":
        listof_partial = schema_partial or model_name in SEARCH_MODELS or model_name in STRUCTURED_LISTOF_PARTIAL_MODELS
        return "partial" if listof_partial else "pass"
    if probe_key.startswith("tool_"):
        if model_name in SEARCH_MODELS or model_name in SMALL_TIER_MODELS or model_name in TOOL_SKIP_MODELS:
            return "skip"
        if any(deployment.deployment_id in TOOL_PARTIAL_DEPLOYMENTS for deployment in deployments):
            return "partial"
        tool_plus_structured_partial = schema_partial or model_name in TOOL_PLUS_STRUCTURED_PARTIAL_MODELS
        return "partial" if probe_key == "tool_plus_structured" and tool_plus_structured_partial else "pass"
    if probe_key == "pdf_attach" and model_name in PDF_RESPONSES_API_BROKEN_MODELS:
        return "fail"
    if probe_key in {"image_attach_and_split", "pdf_attach"}:
        return "pass" if model_name in VISION_MODELS else "skip"
    if probe_key == "search_citations":
        return "pass" if model_name in SEARCH_MODELS else "skip"
    if probe_key == "cache_plumbing" and model_name in CACHE_PARTIAL_MODELS:
        return "partial"
    if probe_key == "reasoning":
        return "skip" if model_name in REASONING_SKIP_MODELS else "pass"
    if probe_key == "replay_round_trip" and model_name in REPLAY_RESPONSES_API_BROKEN_MODELS:
        return "fail"
    return "pass"


def _deployments_by_model(inventory: BenchmarkInventory) -> dict[str, tuple[BenchmarkDeployment, ...]]:
    grouped: dict[str, list[BenchmarkDeployment]] = {}
    for deployment in inventory.concrete_deployments:
        grouped.setdefault(deployment.model_name, []).append(deployment)
    return {model_name: tuple(deployments) for model_name, deployments in grouped.items()}
