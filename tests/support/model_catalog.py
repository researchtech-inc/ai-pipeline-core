"""Shared model fixtures for tests."""

import pytest

from ai_pipeline_core.llm import AIModel

DEFAULT_TEST_MODEL = AIModel(
    name="gemini-3-flash",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=True,
    supports_pdfs=True,
)
ALTERNATE_TEST_MODEL = AIModel(
    name="gpt-5.4-mini",
    supports_structured_output=True,
    supports_tools=True,
    supports_images=True,
    supports_pdfs=True,
)
TEST_MODEL_SET = (DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL)


@pytest.fixture
def default_test_model() -> AIModel:
    """Return the default test model."""
    return DEFAULT_TEST_MODEL


@pytest.fixture
def alternate_test_model() -> AIModel:
    """Return the alternate test model."""
    return ALTERNATE_TEST_MODEL


@pytest.fixture(params=TEST_MODEL_SET, ids=lambda m: m.name)
def test_model_choice(request: pytest.FixtureRequest) -> AIModel:
    """Return each test model in turn."""
    return request.param
