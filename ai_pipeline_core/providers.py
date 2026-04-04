"""Base infrastructure for external service providers.

Provides managed HTTP client lifecycle, transport-level retries,
and test override capability. Subclass and add domain-specific methods.

Module-level instances of subclasses are infrastructure singletons
(see CLAUDE.md §1.2 category 2).
"""

import asyncio
import logging
import time
from abc import abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar

import httpx

from ai_pipeline_core._base_exceptions import NonRetriableError, PipelineCoreError
from ai_pipeline_core.pipeline._execution_context import add_cost

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalProvider",
    "ProviderAuthError",
    "ProviderError",
    "ProviderOutcome",
    "StatelessPollingProvider",
]


# ── Exceptions ────────────────────────────────────────────────


class ProviderError(PipelineCoreError):
    """External provider call failed after all retries.

    Attributes:
        status_code: HTTP status code that caused the failure, or None
                     for connection/timeout errors.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthError(ProviderError, NonRetriableError):
    """Authentication/authorization failure (401/403). Never retried.

    Subclasses both ProviderError (for provider-specific catching)
    and NonRetriableError (so PipelineTask retry loops stop immediately).
    """


# ── Outcome ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProviderOutcome[T]:
    """Terminal result from a stateless provider call.

    Generic over the value type: ``ProviderOutcome[dict]`` for serialized
    documents, ``ProviderOutcome[str]`` for raw text, etc.

    Attributes:
        key: Correlation identifier (normalized URL, provider:prompt, etc.)
        status: Terminal status string from the external service.
        value: Domain-specific result, or None on failure/timeout.
        error: Error description, or None on success.
        cost_usd: Provider-reported cost, or None if unavailable.
    """

    key: str
    status: str
    value: T | None = None
    error: str | None = None
    cost_usd: float | None = None


# ── Base Provider ─────────────────────────────────────────────


class ExternalProvider:
    """Base class for stateless external service providers.

    Manages one shared ``httpx.AsyncClient`` per instance. Lazy-initialized
    on first request. Thread-safe initialization via ``threading.Lock``
    (safe across event loops in test runners).

    Subclasses define domain-specific methods that call ``post_json()``
    and ``get_json()``. Do not store business data on the instance.

    Infrastructure singleton rules (CLAUDE.md §1.2 category 2):
        - Assign once at module scope, never reassign.
        - Internal state never leaks to callers.
        - Caller-stateless: same input → same output regardless of prior calls.
        - Expose ``override()`` for test replacement.
        - Annotate with ``# infrastructure singleton``.
    """

    default_timeout: ClassVar[float] = 30.0
    default_retries: ClassVar[int] = 2
    retry_delay: ClassVar[float] = 1.0
    retry_on_statuses: ClassVar[frozenset[int]] = frozenset({429, 502, 503, 504})

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._init_lock = Lock()
        self._override_var: ContextVar[Any | None] = ContextVar(
            f"_provider_override_{type(self).__name__}",
            default=None,
        )

    # ── Client lifecycle ──────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the shared HTTP client. Thread-safe."""
        if self._client is not None:
            return self._client
        with self._init_lock:
            if self._client is None:
                headers: dict[str, str] = {}
                if self._api_key:
                    headers["Authorization"] = f"Bearer {self._api_key}"
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    headers=headers,
                    timeout=self.default_timeout,
                )
            return self._client

    async def close(self) -> None:
        """Shut down the HTTP client. Call from deployment shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Test override ─────────────────────────────────────

    @contextmanager
    def override(self, backend: Any) -> Generator[None]:
        """Replace this provider's backend for testing.

        The backend is duck-typed: if it has an async ``post(path, payload)``
        method, ``_request_json`` routes through it. Subclasses can also
        check ``_get_override()`` for higher-level interception.

        Usage::

            mock = MockBackend()
            with provider.override(mock):
                result = await provider.some_method(...)
        """
        token: Token[Any | None] = self._override_var.set(backend)
        try:
            yield
        finally:
            self._override_var.reset(token)

    def _get_override(self) -> Any | None:
        """Return the active test override, or None in production."""
        return self._override_var.get()

    # ── HTTP convenience ──────────────────────────────────

    async def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        request_timeout: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        """POST JSON, return parsed response dict. Transport retries included."""
        return await self._request_json(
            "POST",
            path,
            json_body=payload,
            request_timeout=request_timeout,
            retries=retries,
        )

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        request_timeout: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        """GET, return parsed JSON response dict. Transport retries included."""
        return await self._request_json(
            "GET",
            path,
            params=params,
            request_timeout=request_timeout,
            retries=retries,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_timeout: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        """HTTP request → parsed JSON dict with transport-level retries.

        Retries on: connection errors, timeouts, ``retry_on_statuses``.
        Does NOT retry 401/403 (raises ``ProviderAuthError`` immediately).
        Does NOT retry other 4xx (raises ``ProviderError`` immediately).
        """
        override = self._get_override()
        if override is not None and hasattr(override, "post"):
            return await override.post(path, json_body or {})

        client = self._ensure_client()
        effective_timeout = request_timeout if request_timeout is not None else self.default_timeout
        max_attempts = max(retries if retries is not None else self.default_retries, 1)
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    timeout=effective_timeout,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    delay = self.retry_delay * (2**attempt)
                    logger.debug(
                        "%s %s failed (%s), retry %d/%d in %.1fs",
                        method,
                        path,
                        type(exc).__name__,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ProviderError(
                    f"All {max_attempts} attempts failed for {method} {path}: {exc}",
                ) from exc

            if response.status_code in {401, 403}:
                raise ProviderAuthError(
                    f"Authentication failed ({response.status_code}) at {path}",
                    status_code=response.status_code,
                )

            if response.status_code in self.retry_on_statuses and attempt < max_attempts - 1:
                delay = self.retry_delay * (2**attempt)
                logger.debug(
                    "%s %s returned %d, retry %d/%d in %.1fs",
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise ProviderError(
                    f"HTTP {response.status_code} from {path}",
                    status_code=response.status_code,
                )

            return response.json()

        raise ProviderError(
            f"All {max_attempts} attempts failed for {method} {path}",
        ) from last_error

    # ── Cost helper ───────────────────────────────────────

    def _track_cost(self, amount: float | None, *, reason: str = "") -> None:
        """Record provider cost in the current execution span.

        No-ops on None, zero, or negative amounts.
        """
        if amount is not None and amount > 0:
            add_cost(amount, reason=reason)


# ── Stateless Polling Provider ────────────────────────────────


class StatelessPollingProvider[RequestT, ResultT](ExternalProvider):
    """Base for providers using submit-then-poll with content-key correlation.

    Subclasses implement:
        ``submit(request)`` → key: submit work, return the polling key.
        ``poll_once(key)`` → ``ProviderOutcome[ResultT] | None``: check status once.

    The ``call()`` method wires these together with timeout handling.

    Not all providers need this — use plain ``ExternalProvider`` for
    synchronous request/response services (document conversion,
    vector search, etc.).
    """

    poll_interval: ClassVar[float] = 2.0
    terminal_statuses: ClassVar[frozenset[str]] = frozenset({"completed", "failed", "unknown"})

    @abstractmethod
    async def submit(self, request: RequestT) -> str:
        """Submit work to the external service.

        Returns the deterministic key used for later polling.
        The key is derived from request content (URL, query, etc.),
        not from a server-assigned handle.
        """
        ...

    @abstractmethod
    async def poll_once(self, key: str) -> ProviderOutcome[ResultT] | None:
        """Check status once for a previously submitted key.

        Returns ``ProviderOutcome`` when the key has reached a terminal
        status. Returns ``None`` when still running. The implementation
        should call ``self._track_cost()`` on the outcome when ``cost_usd``
        is present.
        """
        ...

    def _cost_reason(self, request: RequestT, key: str) -> str:
        """Build cost tracking label. Override for domain-specific labels."""
        return f"{type(self).__name__}:{key[:80]}"

    async def call(
        self,
        request: RequestT,
        *,
        wait: float = 0,
    ) -> ProviderOutcome[ResultT] | None:
        """Submit, then optionally poll until terminal or timeout.

        ``wait=0``: fire-and-forget — submit only, return None.
        ``wait>0``: submit, then poll until terminal or timeout.

        On timeout, returns a ``ProviderOutcome`` with ``status="timeout"``.
        Does not raise on timeout — the caller decides whether
        timeout is an error for their use case.
        """
        key = await self.submit(request)
        if wait <= 0:
            return None
        return await self._poll_until(key, request=request, max_wait=wait)

    async def _poll_until(
        self,
        key: str,
        *,
        request: RequestT,
        max_wait: float,
        interval: float | None = None,
    ) -> ProviderOutcome[ResultT]:
        """Poll until terminal status or timeout.

        This method handles only the timing loop. It does not
        acquire concurrency slots or track costs — those are
        the subclass's responsibility in ``submit`` and ``poll_once``.

        Returns ``ProviderOutcome`` with ``status="timeout"`` on expiry.
        """
        effective_interval = interval if interval is not None else self.poll_interval
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            outcome = await self.poll_once(key)
            if outcome is not None:
                self._track_cost(outcome.cost_usd, reason=self._cost_reason(request, key))
                return outcome
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(effective_interval, remaining))

        return ProviderOutcome(
            key=key,
            status="timeout",
            error=f"Provider poll timed out after {max_wait}s",
        )
