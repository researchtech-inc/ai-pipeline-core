"""Frozen models for LLM benchmark runs."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_pipeline_core.llm import AIModel

ModelNames = tuple[str, ...]
ProbeKey = Literal[
    "basic_generation",
    "aipl_metadata",
    "cost_sync",
    "routing_telemetry",
    "structured_basemodel",
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
    "concurrency_smoke",
]
ProbeStatus = Literal["pass", "partial", "fail", "skip"]
ProbeScope = Literal["deployment", "model", "global"]

BENCHMARK_MODEL_SET: ModelNames = ("gemini-3-flash", "gpt-5.4-mini")
RENDERER_TEST_MODEL = BENCHMARK_MODEL_SET[0]
RUNNER_STATUS_TEST_MODEL = BENCHMARK_MODEL_SET[1]


class BenchmarkDeployment(BaseModel):
    """One LiteLLM model_list entry parsed from production YAML."""

    model_config = ConfigDict(frozen=True)

    model_name: str
    deployment_id: str
    litellm_model: str
    order: int
    provider_pin: str | None = None
    mode: str | None = None
    cache_kind: str | None = None
    supports_json_schema: bool = True
    cost_optimized: bool = False
    is_wildcard: bool = False


class BenchmarkInventory(BaseModel):
    """Parsed LiteLLM inventory used to build benchmark cases."""

    model_config = ConfigDict(frozen=True)

    config_path: str
    config_sha256: str
    deployments: tuple[BenchmarkDeployment, ...]

    @property
    def concrete_deployments(self) -> tuple[BenchmarkDeployment, ...]:
        """Return non-wildcard deployments."""
        return tuple(deployment for deployment in self.deployments if not deployment.is_wildcard)

    @property
    def wildcard_deployments(self) -> tuple[BenchmarkDeployment, ...]:
        """Return wildcard inventory entries."""
        return tuple(deployment for deployment in self.deployments if deployment.is_wildcard)

    @property
    def model_names(self) -> tuple[str, ...]:
        """Return concrete logical model names in YAML order."""
        return tuple(dict.fromkeys(deployment.model_name for deployment in self.concrete_deployments))


class BenchmarkCase(BaseModel):
    """One model/probe benchmark cell."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    case_id: str
    probe_key: ProbeKey
    scope: ProbeScope
    model: AIModel
    expected: ProbeStatus
    deployment: BenchmarkDeployment | None = None
    nonce: str

    @property
    def model_name(self) -> str:
        """Return the logical model name."""
        return self.model.name

    @property
    def deployment_id(self) -> str | None:
        """Return the forced deployment id when this is a deployment-level case."""
        return self.deployment.deployment_id if self.deployment is not None else None


class ProbeResult(BaseModel):
    """Result for one benchmark probe cell."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    probe_key: ProbeKey
    scope: ProbeScope
    model_name: str
    deployment_id: str | None
    expected: ProbeStatus
    actual: ProbeStatus
    elapsed_ms: int
    cost_usd: float | None = None
    aipl_call_id: str | None = None
    aipl_deployment_id: str | None = None
    provider: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    notes: tuple[str, ...] = ()


class BenchmarkRunManifest(BaseModel):
    """Metadata for one benchmark run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    proxy_base_url_hash: str
    proxy_config_path: str
    proxy_config_sha256: str
    total_cost_usd: float = 0.0
    total_calls: int = 0
    git_sha: str = ""
    framework_version: str = ""


class BenchmarkRun(BaseModel):
    """Complete benchmark run artifact."""

    model_config = ConfigDict(frozen=True)

    manifest: BenchmarkRunManifest
    inventory: BenchmarkInventory
    cases: tuple[BenchmarkCase, ...]
    results: tuple[ProbeResult, ...]
