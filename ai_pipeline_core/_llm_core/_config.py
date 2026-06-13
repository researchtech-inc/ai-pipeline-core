"""Configuration boundary for ``_llm_core``.

``_llm_core`` runs without depending on ``ai_pipeline_core.settings``. The
integrating package installs a populated ``LLMCoreConfig`` once at boot via
``configure(...)``; module code reads it through ``get_config()`` whenever
it needs proxy URL, credentials, or retry defaults. Tests can use
``override_config(...)`` as a contextmanager to swap configuration for one
test.

``configure()`` writes a process-wide default that survives across async
contexts — Prefect remote workers spawn flow runs in detached contexts,
and a ContextVar-only default would silently regress to "unconfigured" in
those tasks. ``override_config()`` layers a per-context override via a
``ContextVar`` so per-test isolation still works without leaking across
concurrent test workers.

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
from threading import Lock
from urllib.parse import urlsplit

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


_DIRECT_PROVIDER_HOSTS: tuple[str, ...] = ("api.openai.com", "openrouter.ai")


@dataclass(frozen=True, slots=True)
class LLMCoreConfig:
    """All external configuration ``_llm_core`` needs to operate.

    Created by the integrating application from its own settings layer.
    Frozen so a single instance can be safely shared across requests.
    """

    openai_base_url: str = ""
    openai_api_key: str = ""
    project: str = ""
    # HTTP connection-pool sizing for the shared AsyncOpenAI client. The 200/100
    # defaults suit a distributed worker fleet (per-process pool); a single-process
    # high-throughput driver can raise them. ``keepalive_expiry`` is 60s to match the
    # proxy's keepalive posture and avoid stale-socket reuse.
    http_max_connections: int = 200
    http_max_keepalive_connections: int = 100
    http_keepalive_expiry_s: float = 60.0
    conversation_retries: int = 2
    conversation_retry_delay_seconds: float = 30.0
    conversation_retry_backoff_multiplier: float = 3.0
    conversation_retry_max_delay_seconds: float = 300.0
    prompt_contract_max_repair: int = 2

    def __post_init__(self) -> None:
        """Validate connection-pool knobs at config load, not at client construction."""
        if self.http_max_connections < 1:
            raise ValueError(
                f"http_max_connections must be >= 1; got {self.http_max_connections}. "
                "Set a positive HTTP_MAX_CONNECTIONS (200 is the per-process default)."
            )
        if self.http_max_keepalive_connections < 0:
            raise ValueError(f"http_max_keepalive_connections must be >= 0; got {self.http_max_keepalive_connections}.")
        if self.http_keepalive_expiry_s <= 0:
            raise ValueError(
                f"http_keepalive_expiry_s must be > 0; got {self.http_keepalive_expiry_s}. "
                "Use a positive idle-keepalive in seconds (60 matches the proxy)."
            )

    @property
    def aipl_proxy(self) -> bool:
        """True when the configured base URL targets the AIPL proxy."""
        base_url = self.openai_base_url.strip()
        if not base_url:
            return True
        host = (urlsplit(base_url if "://" in base_url else f"https://{base_url}").hostname or "").lower()
        return not any(host == direct or host.endswith(f".{direct}") for direct in _DIRECT_PROVIDER_HOSTS)


# Process-wide default config installed by ``configure()``. A ContextVar
# alone would only bind the value in the asyncio context that called
# ``configure()`` — Prefect spawns flow runs in detached contexts that
# would then see ``None`` and raise ``LLMCoreNotConfiguredError`` even
# though boot ran fine in the worker's main context. The single-element
# list lets us mutate the active value without reassigning the module
# binding (which the framework's mutability rules forbid).
_default_config_holder: list[LLMCoreConfig | None] = [None]  # infrastructure singleton
_default_config_lock = Lock()  # infrastructure singleton
# Per-context override layer for ``override_config()`` test isolation.
_override_config: ContextVar[LLMCoreConfig | None] = ContextVar("_llm_core_config_override", default=None)
# infrastructure singleton: cache-invalidation listener list, populated at
# module import via register_on_config_change().
_on_config_change: list[Callable[[], None]] = []  # nosemgrep: no-mutable-module-globals - import-time registry


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

    Writes the process-wide default — survives across asyncio contexts
    (including Prefect remote-worker flow runs and child tasks spawned
    with detached contexts).
    """
    with _default_config_lock:
        _default_config_holder[0] = config
    for listener in _on_config_change:
        listener()


def get_config() -> LLMCoreConfig:
    """Return the active config, or raise if unconfigured.

    Always succeeds inside ``ai_pipeline_core`` (the bootstrap module runs
    ``configure()`` at package import). Standalone consumers must call
    ``configure(LLMCoreConfig(...))`` before any ``_llm_core`` function.
    """
    override = _override_config.get()
    if override is not None:
        return override
    config = _default_config_holder[0]
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
    token = _override_config.set(config)
    try:
        yield
    finally:
        _override_config.reset(token)
