"""Shared fixtures for prompt_compiler tests."""

import pytest


@pytest.fixture(autouse=True)
def _suppress_registration():
    return
