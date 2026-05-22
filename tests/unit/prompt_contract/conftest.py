"""Shared fixtures for prompt_contract tests."""

import pytest


@pytest.fixture(autouse=True)
def _suppress_registration():
    return
