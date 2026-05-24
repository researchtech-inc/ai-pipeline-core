"""Shared model fixtures for tests."""

import pytest

from ai_pipeline_core.llm import AIModel

DEFAULT_TEST_MODEL = AIModel(
    name="gemini-3-flash",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=True,
    supports_pdfs=True,
    supports_json_schema=True,
)
ALTERNATE_TEST_MODEL = AIModel(
    name="gpt-5.4-mini",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=True,
    supports_pdfs=True,
    supports_json_schema=True,
)
# Catalog models for tests that exercise schema injection: declare
# ``supports_json_schema=False`` so the framework appends the prose schema
# description to the last USER message, matching the production fallback
# path for models that do not honor strict ``json_schema``.
# ``deepseek-v4-flash`` and ``mimo-v2.5`` are chosen for low latency.
WEAK_SCHEMA_TEST_MODEL = AIModel(
    name="deepseek-v4-flash",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=False,
    supports_pdfs=False,
    supports_json_schema=False,
)
ALTERNATE_WEAK_SCHEMA_TEST_MODEL = AIModel(
    name="mimo-v2.5",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=False,
    supports_pdfs=False,
    supports_json_schema=False,
)
WEAK_SCHEMA_TEST_MODEL_SET = (WEAK_SCHEMA_TEST_MODEL, ALTERNATE_WEAK_SCHEMA_TEST_MODEL)
TEST_MODEL_SET = (DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL)

# Search-enabled variants. ``timeout_s`` is widened so the watchdog's
# ``total_wall_seconds`` (sourced from ``RetrySpec.timeout_s``) covers the
# long upstream search phases (200-1200s for deep-research workloads;
# integration smoke prompts complete in ~30-90s).
GPT_NANO_SEARCH_TEST_MODEL = AIModel(
    name="gpt-5.4-nano-search",
    preserve_input_urls=True,
    supports_tools=True,
    supports_structured_output=True,
    supports_json_schema=True,
    timeout_s=600.0,
    reasoning_effort="low",
)
GEMINI_FLASH_SEARCH_TEST_MODEL = AIModel(
    name="gemini-3-flash-search",
    preserve_input_urls=True,
    supports_tools=True,
    supports_structured_output=True,
    supports_json_schema=True,
    timeout_s=600.0,
)


@pytest.fixture
def default_test_model() -> AIModel:
    """Return the default test model."""
    return DEFAULT_TEST_MODEL


@pytest.fixture
def alternate_test_model() -> AIModel:
    """Return the alternate test model."""
    return ALTERNATE_TEST_MODEL


@pytest.fixture
def weak_schema_test_model() -> AIModel:
    """Return a model with ``supports_json_schema=False`` for injection tests."""
    return WEAK_SCHEMA_TEST_MODEL


@pytest.fixture
def alternate_weak_schema_test_model() -> AIModel:
    """Return an alternate model with ``supports_json_schema=False`` for injection tests."""
    return ALTERNATE_WEAK_SCHEMA_TEST_MODEL


@pytest.fixture(params=WEAK_SCHEMA_TEST_MODEL_SET, ids=lambda m: m.name)
def weak_schema_model_choice(request: pytest.FixtureRequest) -> AIModel:
    """Return each weak-schema test model in turn."""
    return request.param


@pytest.fixture(params=TEST_MODEL_SET, ids=lambda m: m.name)
def test_model_choice(request: pytest.FixtureRequest) -> AIModel:
    """Return each test model in turn."""
    return request.param


@pytest.fixture
def gpt_nano_search_test_model() -> AIModel:
    """Search-enabled GPT model fixture (`gpt-5.4-nano-search`)."""
    return GPT_NANO_SEARCH_TEST_MODEL


@pytest.fixture
def gemini_flash_search_test_model() -> AIModel:
    """Search-enabled Gemini model fixture (`gemini-3-flash-search`)."""
    return GEMINI_FLASH_SEARCH_TEST_MODEL
