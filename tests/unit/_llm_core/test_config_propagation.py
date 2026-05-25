"""Tests for ``_llm_core._config`` cross-context propagation.

Regression for the Prefect remote-worker bootstrap bug: ``configure()``
must install a *process-wide* default that survives across detached
asyncio contexts. A ContextVar-only default would silently regress to
"unconfigured" in any task spawned in a context that did not inherit
from the original ``configure()`` call.
"""

import asyncio
import contextvars
from collections.abc import Generator

import pytest

from ai_pipeline_core._llm_core._config import (
    LLMCoreConfig,
    LLMCoreNotConfiguredError,
    configure,
    get_config,
    override_config,
)


@pytest.fixture(autouse=True)
def _restore_default_config() -> Generator[None]:
    """Snapshot and restore the process-wide default around each test.

    The framework's import-time bootstrap installs a real default we must
    not clobber for other tests in the lane.
    """
    import ai_pipeline_core._llm_core._config as config_module

    saved = config_module._default_config_holder[0]
    yield
    config_module._default_config_holder[0] = saved


def test_configure_installs_default_visible_from_fresh_context() -> None:
    """A fresh ``contextvars.Context`` (no inheritance from the caller)
    must still see the installed default. Models Prefect's remote worker
    spawning flow runs in detached contexts that don't inherit from the
    process import context.
    """
    sentinel = LLMCoreConfig(openai_base_url="http://proxy/v1", openai_api_key="sk-detached-test")
    configure(sentinel)

    seen: list[LLMCoreConfig] = []

    def _read_in_fresh_context() -> None:
        seen.append(get_config())

    fresh = contextvars.Context()
    fresh.run(_read_in_fresh_context)

    assert seen == [sentinel]


def test_configure_visible_from_asyncio_run_root_task() -> None:
    """``asyncio.run`` may copy a fresh context; verify the default survives."""
    sentinel = LLMCoreConfig(openai_base_url="http://proxy/v1", openai_api_key="sk-asyncio-test")
    configure(sentinel)

    async def _read() -> LLMCoreConfig:
        return get_config()

    result = asyncio.run(_read())
    assert result is sentinel


def test_override_config_layers_on_top_of_default_without_overwriting() -> None:
    """``override_config()`` is per-context. Leaving the context restores the
    process-wide default for both the current and any sibling contexts.
    """
    process_default = LLMCoreConfig(openai_base_url="http://proxy/v1", openai_api_key="sk-default")
    override = LLMCoreConfig(openai_base_url="http://test/v1", openai_api_key="sk-override")
    configure(process_default)

    with override_config(override):
        assert get_config() is override

    assert get_config() is process_default


def test_override_does_not_leak_to_sibling_context() -> None:
    """Per-test override isolation: a sibling context never sees the override."""
    process_default = LLMCoreConfig(openai_base_url="http://proxy/v1", openai_api_key="sk-default")
    override = LLMCoreConfig(openai_base_url="http://test/v1", openai_api_key="sk-override")
    configure(process_default)

    sibling_seen: list[LLMCoreConfig] = []

    def _read_sibling() -> None:
        sibling_seen.append(get_config())

    with override_config(override):
        contextvars.Context().run(_read_sibling)

    assert sibling_seen == [process_default]


def test_get_config_raises_when_uninstalled() -> None:
    """Before ``configure()`` runs, ``get_config()`` raises a clear error."""
    import ai_pipeline_core._llm_core._config as config_module

    config_module._default_config_holder[0] = None
    with pytest.raises(LLMCoreNotConfiguredError):
        get_config()
