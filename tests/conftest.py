"""Common test fixtures for pipeline projects."""

import logging
import os
import shutil
import subprocess
import warnings
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import pytest

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.llm import AIModel
from ai_pipeline_core.pipeline._execution_context import _RunContext
from ai_pipeline_core.pipeline._execution_context import set_run_context
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

AIModel.model_fields["skip_cost_optimized"].default = True
AIModel.model_rebuild(force=True)

pytest_plugins = ("tests.support.helpers", "tests.support.model_catalog")


class _SQLAlchemyConnectionFilter(logging.Filter):
    """Suppress SQLAlchemy connection termination errors during Prefect shutdown."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "sqlalchemy.pool.impl.AsyncAdaptedQueuePool":
            if "Exception terminating connection" in record.getMessage():
                return False
        if "aiosqlite" in record.name and "CancelledError" in record.getMessage():
            return False
        return True


@pytest.fixture(autouse=True, scope="session")
def prefect_test_fixture():
    """Session-scoped Prefect ephemeral database shared across all test directories."""
    sqlalchemy_logger = logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool")
    aiosqlite_logger = logging.getLogger("aiosqlite")
    filter_instance = _SQLAlchemyConnectionFilter()
    sqlalchemy_logger.addFilter(filter_instance)
    aiosqlite_logger.addFilter(filter_instance)
    warnings.filterwarnings("ignore", message=".*CancelledError.*", category=Warning)
    warnings.filterwarnings("ignore", message=".*unclosed.*", category=ResourceWarning)
    try:
        with prefect_test_harness():
            yield
    finally:
        sqlalchemy_logger.removeFilter(filter_instance)
        aiosqlite_logger.removeFilter(filter_instance)


@pytest.fixture(autouse=True)
def disable_prefect_logging():
    """Disable Prefect run logger to prevent RuntimeError from missing flow context."""
    with disable_run_logger():
        yield


@pytest.fixture(autouse=True)
def disable_doc_summary_generation():
    """Disable LLM-based document summary generation in tests by default.

    Auto-summary calls Conversation.send() which interferes with tests that use
    FakeLLMClient or count conversation nodes. Tests that explicitly test summary
    generation should override settings or patch _generate_summaries.
    """
    from ai_pipeline_core.settings import settings

    if not settings.doc_summary_enabled:
        yield
        return
    object.__setattr__(settings, "doc_summary_enabled", False)  # noqa: PLC2801 — frozen Pydantic model
    try:
        yield
    finally:
        object.__setattr__(settings, "doc_summary_enabled", True)  # noqa: PLC2801 — frozen Pydantic model


@pytest.fixture(autouse=True, scope="session")
def isolate_from_env_clickhouse():
    """Prevent tests from connecting to .env ClickHouse.

    Forces create_database_from_settings() to fall back to MemoryDatabase
    unless a test explicitly creates its own Settings and database.
    Tests that need real ClickHouse (test_clickhouse_integration.py) build
    their own Settings with explicit clickhouse_host and pass database= directly.
    """
    from ai_pipeline_core.settings import settings

    original = settings.clickhouse_host
    object.__setattr__(settings, "clickhouse_host", "")  # noqa: PLC2801 — frozen Pydantic model
    yield
    object.__setattr__(settings, "clickhouse_host", original)  # noqa: PLC2801 — frozen Pydantic model


@pytest.fixture
def run_context():
    """Provide a RunContext with a deterministic run_id and set it via ContextVar.

    Automatically resets the ContextVar after the test.
    """
    ctx = _RunContext(run_id="test-run-scope")
    with set_run_context(ctx):
        yield ctx


# ---------------------------------------------------------------------------
# Infrastructure availability detection
# ---------------------------------------------------------------------------

DOCKER_INFO_TIMEOUT_SECONDS = 10


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """Check once per session whether Docker daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=DOCKER_INFO_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):  # fmt: skip
        return False


@pytest.fixture(scope="session")
def require_docker(docker_available: bool) -> None:
    """Skip the entire test session/module if Docker is not available."""
    if not docker_available:
        pytest.skip("Docker not available — install Docker or start the daemon")


_COST_LIMIT_ENV = "INTEGRATION_COST_LIMIT_USD"
_DEFAULT_COST_LIMIT_USD = 5.0


@dataclass(slots=True)
class _CostBudget:
    """Track integration test spend and fail if the session exceeds the limit."""

    limit_usd: float
    total_usd: float = 0.0

    def add(self, value: Any) -> None:
        """Add a response or conversation cost to the budget."""
        if hasattr(value, "messages"):
            response_costs = [message.cost for message in value.messages if isinstance(message, ModelResponse)]
            if response_costs:
                if any(cost is None for cost in response_costs):
                    raise AssertionError(
                        "Tracked successful integration LLM conversation did not report every round cost."
                    )
                total_cost = sum(float(cost) for cost in response_costs if cost is not None)
                if total_cost < 0:
                    raise AssertionError(f"Tracked integration cost must be non-negative, got {total_cost}.")
                self.total_usd += total_cost
                return

        cost = value if isinstance(value, int | float) else getattr(value, "cost", None)
        if cost is None:
            raise AssertionError("Tracked successful integration LLM call did not report a cost.")
        if cost < 0:
            raise AssertionError(f"Tracked integration cost must be non-negative, got {cost}.")
        self.total_usd += float(cost)


@pytest.fixture(scope="session", autouse=True)
def cost_budget() -> Generator[_CostBudget]:
    """Fail the integration session if successful live-call costs exceed the budget."""
    limit = float(os.environ.get(_COST_LIMIT_ENV, _DEFAULT_COST_LIMIT_USD))
    budget = _CostBudget(limit_usd=limit)
    yield budget
    if budget.total_usd > budget.limit_usd:
        raise AssertionError(f"Integration LLM cost ${budget.total_usd:.4f} exceeded budget ${budget.limit_usd:.2f}.")
