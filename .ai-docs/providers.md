# MODULE: providers
# CLASSES: ProviderError, ProviderAuthError, ProviderOutcome, ExternalProvider, StatelessPollingProvider
# DEPENDS: Exception
# VERSION: 0.21.0
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import ExternalProvider, ProviderAuthError, ProviderError, ProviderOutcome, StatelessPollingProvider
```

## Public API

```python
class ProviderError(PipelineCoreError):
    """External provider call failed after all retries.

    Attributes:
        status_code: HTTP status code that caused the failure, or None
                     for connection/timeout errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthError(ProviderError, NonRetriableError):
    """Authentication/authorization failure (401/403). Never retried.

    Subclasses both ProviderError (for provider-specific catching)
    and NonRetriableError (so PipelineTask retry loops stop immediately)."""

    # [Inherited from ProviderError]
    # __init__


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """Terminal result from a stateless provider call.

    Generic over the value type: ``ProviderOutcome[dict]`` for serialized
    documents, ``ProviderOutcome[str]`` for raw text, etc.

    Attributes:
        key: Correlation identifier (normalized URL, provider:prompt, etc.)
        status: Terminal status string from the external service.
        value: Domain-specific result, or None on failure/timeout.
        error: Error description, or None on success.
        cost_usd: Provider-reported cost, or None if unavailable."""

    key: str
    status: str
    value: T | None = None
    error: str | None = None
    cost_usd: float | None = None


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
        - Annotate with ``# infrastructure singleton``."""

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

    async def close(self) -> None:
        """Shut down the HTTP client. Call from deployment shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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


class StatelessPollingProvider(ExternalProvider):
    """Base for providers using submit-then-poll with content-key correlation.

    Subclasses implement:
        ``submit(request)`` → key: submit work, return the polling key.
        ``poll_once(key)`` → ``ProviderOutcome[ResultT] | None``: check status once.

    The ``call()`` method wires these together with timeout handling.

    Not all providers need this — use plain ``ExternalProvider`` for
    synchronous request/response services (document conversion,
    vector search, etc.)."""

    poll_interval: ClassVar[float] = 2.0
    terminal_statuses: ClassVar[frozenset[str]] = frozenset({"completed", "failed", "unknown"})

    # [Inherited from ExternalProvider]
    # __init__, close, get_json, override, post_json

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

    @abstractmethod
    async def poll_once(self, key: str) -> ProviderOutcome[ResultT] | None:
        """Check status once for a previously submitted key.

        Returns ``ProviderOutcome`` when the key has reached a terminal
        status. Returns ``None`` when still running. The implementation
        should call ``self._track_cost()`` on the outcome when ``cost_usd``
        is present.
        """
        ...

    @abstractmethod
    async def submit(self, request: RequestT) -> str:
        """Submit work to the external service.

        Returns the deterministic key used for later polling.
        The key is derived from request content (URL, query, etc.),
        not from a server-assigned handle.
        """
        ...
```

## Examples

**Importable from providers module** (`tests/test_providers.py:605`)

```python
def test_importable_from_providers_module(self):
    from ai_pipeline_core.providers import ProviderAuthError, ProviderError

    assert ProviderError is not None
    assert ProviderAuthError is not None
```

**All fields** (`tests/test_providers.py:107`)

```python
def test_all_fields(self):
    outcome = ProviderOutcome(key="k", status="completed", value="result", error=None, cost_usd=0.05)
    assert outcome.key == "k"
    assert outcome.status == "completed"
    assert outcome.value == "result"
    assert outcome.cost_usd == 0.05
```

**Base url trailing slash stripped** (`tests/test_providers.py:166`)

```python
def test_base_url_trailing_slash_stripped(self):
    provider = ExternalProvider(base_url="http://test.local/api/")
    assert provider._base_url == "http://test.local/api"
```

**Client created on first request** (`tests/test_providers.py:128`)

```python
@pytest.mark.asyncio
async def test_client_created_on_first_request(self):
    transport = _transport_sequence([_json_response({"ok": True})])
    provider = ExternalProvider(base_url="http://test.local")
    assert provider._client is None
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://test.local")
    result = await provider.post_json("/test", {})
    assert result == {"ok": True}
```

**Defaults** (`tests/test_providers.py:101`)

```python
def test_defaults(self):
    outcome = ProviderOutcome(key="k", status="ok")
    assert outcome.value is None
    assert outcome.error is None
    assert outcome.cost_usd is None
```

**Fire and forget** (`tests/test_providers.py:501`)

```python
@pytest.mark.asyncio
async def test_fire_and_forget(self):
    poller = ConcretePoller(poll_responses=[ProviderOutcome(key="x", status="completed")])

    result = await poller.call("req", wait=0)

    assert result is None
    assert len(poller.submit_calls) == 1
    assert len(poller.poll_calls) == 0
```

**Importable from top level** (`tests/test_providers.py:590`)

```python
def test_importable_from_top_level(self):
    from ai_pipeline_core import (
        ExternalProvider,
        ProviderAuthError,
        ProviderError,
        ProviderOutcome,
        StatelessPollingProvider,
    )

    assert ExternalProvider is not None
    assert StatelessPollingProvider is not None
    assert ProviderOutcome is not None
    assert ProviderError is not None
    assert ProviderAuthError is not None
```

**Lazy client initialization** (`tests/test_providers.py:123`)

```python
def test_lazy_client_initialization(self):
    provider = ExternalProvider(base_url="http://test.local")
    assert provider._client is None
```

**Provider auth error carries status code** (`tests/test_providers.py:87`)

```python
def test_provider_auth_error_carries_status_code(self):
    err = ProviderAuthError("auth fail", status_code=401)
    assert err.status_code == 401
```


## Error Examples

**Frozen** (`tests/test_providers.py:96`)

```python
def test_frozen(self):
    outcome = ProviderOutcome(key="k", status="completed", value={"a": 1})
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.key = "new"  # type: ignore[misc]
```

**401 raises immediately** (`tests/test_providers.py:255`)

```python
@pytest.mark.asyncio
async def test_401_raises_immediately(self):
    request_count = 0

    async def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response({}, status_code=401)

    transport = httpx.MockTransport(counting_handler)
    provider = _make_provider(transport)

    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.post_json("/test", {})

    assert exc_info.value.status_code == 401
    assert request_count == 1
```

**403 raises immediately** (`tests/test_providers.py:273`)

```python
@pytest.mark.asyncio
async def test_403_raises_immediately(self):
    request_count = 0

    async def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response({}, status_code=403)

    transport = httpx.MockTransport(counting_handler)
    provider = _make_provider(transport)

    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.post_json("/test", {})

    assert exc_info.value.status_code == 403
    assert request_count == 1
```

**404 raises provider error no retry** (`tests/test_providers.py:291`)

```python
@pytest.mark.asyncio
async def test_404_raises_provider_error_no_retry(self):
    request_count = 0

    async def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _json_response({}, status_code=404)

    transport = httpx.MockTransport(counting_handler)
    provider = _make_provider(transport)

    with pytest.raises(ProviderError) as exc_info:
        await provider.post_json("/test", {})

    assert not isinstance(exc_info.value, ProviderAuthError)
    assert exc_info.value.status_code == 404
    assert request_count == 1
```

**Timeout retries then raises** (`tests/test_providers.py:193`)

```python
@pytest.mark.asyncio
async def test_timeout_retries_then_raises(self):
    transport = _transport_sequence([
        httpx.TimeoutException("timeout"),
        httpx.TimeoutException("timeout"),
        httpx.TimeoutException("timeout"),
    ])
    provider = _make_provider(transport)
    provider.__class__.default_retries = 3
    try:
        with pytest.raises(ProviderError, match="All 3 attempts failed"):
            await provider.post_json("/test", {})
    finally:
        provider.__class__.default_retries = 2
```
