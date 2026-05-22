"""Shared fixtures for pipeline tests."""

import pytest


@pytest.fixture(autouse=True)
def _suppress_registration():
    return
