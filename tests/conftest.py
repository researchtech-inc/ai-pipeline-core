"""Common test fixtures for pipeline projects."""

import os

# Disable Laminar before any ai_pipeline_core imports — prevents Settings from picking up the key
os.environ.pop("LMNR_PROJECT_API_KEY", None)

import logging
import shutil
import subprocess
import warnings
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.pipeline._execution_context import _RunContext
from ai_pipeline_core.pipeline._execution_context import set_run_context
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from dev_cli._lane import LANE_TIMEOUTS, lane_from_path

pytest_plugins = ("tests.support.helpers", "tests.support.model_catalog")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply lane default timeouts unless a test has an explicit timeout marker."""
    repo_root = Path.cwd().resolve()
    for item in items:
        if item.get_closest_marker("timeout") is not None:
            continue
        try:
            relpath = Path(str(item.fspath)).resolve().relative_to(repo_root).as_posix()
        except ValueError:
            relpath = Path(str(item.fspath)).as_posix()
        lane = lane_from_path(relpath)
        if lane in LANE_TIMEOUTS:
            item.add_marker(pytest.mark.timeout(LANE_TIMEOUTS[lane]))


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
    # clickhouse-connect async client FutureWarning — suppress in tests too (see _connection.py)
    warnings.filterwarnings("ignore", category=FutureWarning, module=r"clickhouse_connect")
    warnings.filterwarnings("ignore", category=FutureWarning, message=r".*async.*client.*")
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
def disable_laminar_tracing():
    """Disable Laminar tracing in tests — prevents SDK initialization and network calls."""
    from ai_pipeline_core.observability._laminar_sink import _reset_for_testing
    from ai_pipeline_core.settings import settings

    _reset_for_testing()
    original = settings.lmnr_project_api_key
    object.__setattr__(settings, "lmnr_project_api_key", "")  # noqa: PLC2801 — frozen Pydantic model
    yield
    object.__setattr__(settings, "lmnr_project_api_key", original)  # noqa: PLC2801 — frozen Pydantic model
    _reset_for_testing()


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
    except OSError, subprocess.TimeoutExpired:
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
                    raise AssertionError("Tracked successful integration LLM conversation did not report every round cost.")
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
