"""Configuration boundary for ``_llm_core``.

``_llm_core`` runs without depending on ``ai_pipeline_core.settings``. The
integrating package installs a populated ``LLMCoreConfig`` once at boot via
``configure(...)``; module code reads it through ``get_config()`` whenever
it needs proxy URL, credentials, or retry defaults. Tests can use
``override_config(...)`` as a contextmanager to swap configuration for one
test.

The ContextVar defaults to ``None`` so unconfigured access raises a clear
``RuntimeError`` instead of silently using empty credentials. Standalone
consumers must call ``configure(LLMCoreConfig(...))`` before any
``_llm_core`` function is invoked. Inside ``ai_pipeline_core`` this is
done at package import by ``_llm_core_bootstrap``.

Cached resources (e.g. the HTTP client) register a listener via
``register_on_config_change(...)`` so ``configure()`` can invalidate them
without ``_config`` importing transport-layer code. ``override_config()``
intentionally does NOT fire the listeners (concurrent contexts would
race); use ``_transport.override_client(...)`` for per-test client swap.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = [
    "LLMCoreConfig",
    "LLMCoreNotConfiguredError",
    "configure",
    "get_config",
    "override_config",
    "register_on_config_change",
]


class LLMCoreNotConfiguredError(RuntimeError):
    """Raised when ``_llm_core`` is used before ``configure()`` was called."""


@dataclass(frozen=True, slots=True)
class LLMCoreConfig:
    """All external configuration ``_llm_core`` needs to operate.

    Created by the integrating application from its own settings layer.
    Frozen so a single instance can be safely shared across requests.
    """

    openai_base_url: str = ""
    openai_api_key: str = ""
    conversation_retries: int = 2
    conversation_retry_delay_seconds: float = 30.0
    conversation_retry_backoff_multiplier: float = 3.0
    conversation_retry_max_delay_seconds: float = 300.0
    prompt_contract_max_repair: int = 2


_config: ContextVar[LLMCoreConfig | None] = ContextVar("_llm_core_config", default=None)
# infrastructure singleton: cache-invalidation listener list, populated at
# module import via register_on_config_change().
_on_config_change: list[Callable[[], None]] = []  # nosemgrep: no-mutable-module-globals  # import-time registry per CLAUDE.md §1.2 category 3


def register_on_config_change(listener: Callable[[], None]) -> None:
    """Register a listener to fire when ``configure()`` installs a new config.

    ``_transport`` registers its cache-invalidation hook here so the
    config module does not need to import transport code (which would
    create a circular import at module load).
    """
    _on_config_change.append(listener)


def configure(config: LLMCoreConfig) -> None:
    """Install the active config for this process.

    Idempotent. Called once at integrator boot
    (``ai_pipeline_core._llm_core_bootstrap``) and never again from
    application code. Fires every registered listener so cached
    resources (HTTP client, etc.) rebuild against the new config.
    """
    _config.set(config)
    for listener in _on_config_change:
        listener()


def get_config() -> LLMCoreConfig:
    """Return the active config, or raise if unconfigured.

    Always succeeds inside ``ai_pipeline_core`` (the bootstrap module runs
    ``configure()`` at package import). Standalone consumers must call
    ``configure(LLMCoreConfig(...))`` before any ``_llm_core`` function.
    """
    config = _config.get()
    if config is None:
        raise LLMCoreNotConfiguredError(
            "_llm_core is not configured. Call _llm_core._config.configure(LLMCoreConfig(...)) "
            "at boot, or import ai_pipeline_core which installs default configuration from Settings."
        )
    return config


@contextmanager
def override_config(config: LLMCoreConfig) -> Iterator[None]:
    """Temporarily swap the active config for the current context.

    Does NOT fire config-change listeners (concurrent contexts would
    race the cached client). For per-test client swap use
    ``_transport.override_client(...)``.
    """
    token = _config.set(config)
    try:
        yield
    finally:
        _config.reset(token)
