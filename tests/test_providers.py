"""Tests for ExternalProvider, StatelessPollingProvider, ProviderOutcome, and provider exceptions."""

import asyncio
import dataclasses
import threading
from unittest.mock import patch

import httpx
import pytest

from ai_pipeline_core._base_exceptions import NonRetriableError, PipelineCoreError
from ai_pipeline_core.providers import (
    ExternalProvider,
    ProviderAuthError,
    ProviderError,
    ProviderOutcome,
    StatelessPollingProvider,
)


# ── Helpers ───────────────────────────────────────────────────


def _transport_sequence(responses: list[httpx.Response | Exception]) -> httpx.MockTransport:
    """Build a MockTransport that returns responses in order."""
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        item = responses[idx]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


def _make_provider(transport: httpx.MockTransport, **kwargs: object) -> ExternalProvider:
    """Create a provider with injected transport (bypasses lazy init)."""
    provider = ExternalProvider(base_url="http://test.local", **kwargs)
    provider._client = httpx.AsyncClient(transport=transport, base_url="http://test.local")
    return provider


def _json_response(data: dict[str, object], status_code: int = 200) -> httpx.Response:
    import json

    return httpx.Response(status_code, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


class RecordingBackend:
    """Mock backend that records calls and returns a fixed response."""

    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._response = response or {"mocked": True}

    async def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((path, payload))
        return self._response


# ── Exception Tests ───────────────────────────────────────────


class TestProviderExceptions:
    def test_provider_error_is_pipeline_core_error(self):
        assert issubclass(ProviderError, PipelineCoreError)

    def test_provider_auth_error_is_provider_error(self):
        assert issubclass(ProviderAuthError, ProviderError)

    def test_provider_auth_error_is_non_retriable(self):
        assert issubclass(ProviderAuthError, NonRetriableError)

    def test_provider_error_carries_status_code(self):
        err = ProviderError("fail", status_code=500)
        assert err.status_code == 500
        assert str(err) == "fail"

    def test_provider_error_status_code_defaults_to_none(self):
        err = ProviderError("fail")
        assert err.status_code is None

    def test_provider_auth_error_carries_status_code(self):
        err = ProviderAuthError("auth fail", status_code=401)
        assert err.status_code == 401


# ── ProviderOutcome Tests ─────────────────────────────────────


class TestProviderOutcome:
    def test_frozen(self):
        outcome = ProviderOutcome(key="k", status="completed", value={"a": 1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.key = "new"  # type: ignore[misc]

    def test_defaults(self):
        outcome = ProviderOutcome(key="k", status="ok")
        assert outcome.value is None
        assert outcome.error is None
        assert outcome.cost_usd is None

    def test_all_fields(self):
        outcome = ProviderOutcome(key="k", status="completed", value="result", error=None, cost_usd=0.05)
        assert outcome.key == "k"
        assert outcome.status == "completed"
        assert outcome.value == "result"
        assert outcome.cost_usd == 0.05

    def test_slots(self):
        outcome = ProviderOutcome(key="k", status="ok")
        assert not hasattr(outcome, "__dict__")


# ── ExternalProvider Tests ────────────────────────────────────


class TestExternalProviderInit:
    def test_lazy_client_initialization(self):
        provider = ExternalProvider(base_url="http://test.local")
        assert provider._client is None

    @pytest.mark.asyncio
    async def test_client_created_on_first_request(self):
        transport = _transport_sequence([_json_response({"ok": True})])
        provider = ExternalProvider(base_url="http://test.local")
        assert provider._client is None
        provider._client = httpx.AsyncClient(transport=transport, base_url="http://test.local")
        result = await provider.post_json("/test", {})
        assert result == {"ok": True}

    def test_thread_safe_initialization(self):
        provider = ExternalProvider(base_url="http://test.local", api_key="key")
        barrier = threading.Barrier(2)
        clients: list[httpx.AsyncClient] = []

        def init_client():
            barrier.wait()
            client = provider._ensure_client()
            clients.append(client)

        t1 = threading.Thread(target=init_client)
        t2 = threading.Thread(target=init_client)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(clients) == 2
        assert clients[0] is clients[1]

    def test_api_key_sets_auth_header(self):
        provider = ExternalProvider(base_url="http://test.local", api_key="secret")
        client = provider._ensure_client()
        assert client.headers["authorization"] == "Bearer secret"

    def test_no_api_key_no_auth_header(self):
        provider = ExternalProvider(base_url="http://test.local")
        client = provider._ensure_client()
        assert "authorization" not in client.headers

    def test_base_url_trailing_slash_stripped(self):
        provider = ExternalProvider(base_url="http://test.local/api/")
        assert provider._base_url == "http://test.local/api"


class TestExternalProviderRetries:
    @pytest.mark.asyncio
    async def test_retry_on_503(self):
        transport = _transport_sequence([
            _json_response({}, status_code=503),
            _json_response({"ok": True}),
        ])
        provider = _make_provider(transport)
        result = await provider.post_json("/test", {})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_retry_on_429(self):
        transport = _transport_sequence([
            _json_response({}, status_code=429),
            _json_response({"ok": True}),
        ])
        provider = _make_provider(transport)
        result = await provider.post_json("/test", {})
        assert result == {"ok": True}

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

    @pytest.mark.asyncio
    async def test_connection_error_retries_then_succeeds(self):
        transport = _transport_sequence([
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            _json_response({"ok": True}),
        ])

        class ThreeRetryProvider(ExternalProvider):
            default_retries = 3

        provider = ThreeRetryProvider(base_url="http://test.local")
        provider._client = httpx.AsyncClient(transport=transport, base_url="http://test.local")
        result = await provider.post_json("/test", {})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self):
        transport = _transport_sequence([
            _json_response({}, status_code=503),
            _json_response({}, status_code=503),
            _json_response({"ok": True}),
        ])

        class FastRetryProvider(ExternalProvider):
            default_retries = 3
            retry_delay = 0.1

        provider = FastRetryProvider(base_url="http://test.local")
        provider._client = httpx.AsyncClient(transport=transport, base_url="http://test.local")

        sleep_durations: list[float] = []
        original_sleep = asyncio.sleep

        async def recording_sleep(duration: float):
            sleep_durations.append(duration)
            await original_sleep(min(duration, 0.01))

        with patch("ai_pipeline_core.providers.asyncio.sleep", side_effect=recording_sleep):
            await provider.post_json("/test", {})

        assert len(sleep_durations) == 2
        assert abs(sleep_durations[0] - 0.1) < 0.05
        assert abs(sleep_durations[1] - 0.2) < 0.05


class TestExternalProviderAuthErrors:
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


class TestExternalProviderOverride:
    @pytest.mark.asyncio
    async def test_override_routes_through_mock(self):
        provider = ExternalProvider(base_url="http://test.local")
        mock = RecordingBackend({"mocked": True})

        with provider.override(mock):
            result = await provider.post_json("/test", {"x": 1})

        assert result == {"mocked": True}
        assert mock.calls == [("/test", {"x": 1})]

    @pytest.mark.asyncio
    async def test_override_isolation_between_concurrent_tasks(self):
        provider = ExternalProvider(base_url="http://test.local")
        mock_a = RecordingBackend({"source": "a"})
        mock_b = RecordingBackend({"source": "b"})
        results: dict[str, dict[str, object]] = {}

        async def task_a():
            with provider.override(mock_a):
                await asyncio.sleep(0.01)
                results["a"] = await provider.post_json("/test", {})

        async def task_b():
            with provider.override(mock_b):
                await asyncio.sleep(0.01)
                results["b"] = await provider.post_json("/test", {})

        await asyncio.gather(task_a(), task_b())

        assert results["a"] == {"source": "a"}
        assert results["b"] == {"source": "b"}

    @pytest.mark.asyncio
    async def test_override_exit_restores_real_client(self):
        transport = _transport_sequence([_json_response({"real": True}), _json_response({"real": True})])
        provider = _make_provider(transport)
        mock = RecordingBackend({"fake": True})

        with provider.override(mock):
            result_fake = await provider.post_json("/test", {})
        result_real = await provider.post_json("/test", {})

        assert result_fake == {"fake": True}
        assert result_real == {"real": True}


class TestExternalProviderClose:
    @pytest.mark.asyncio
    async def test_close_shuts_down_client(self):
        transport = _transport_sequence([_json_response({"ok": True}), _json_response({"ok": True})])
        provider = _make_provider(transport)

        await provider.post_json("/test", {})
        assert provider._client is not None

        await provider.close()
        assert provider._client is None

    @pytest.mark.asyncio
    async def test_close_then_reinit(self):
        provider = ExternalProvider(base_url="http://test.local")
        client1 = provider._ensure_client()
        await provider.close()
        assert provider._client is None
        client2 = provider._ensure_client()
        assert client2 is not client1


class TestExternalProviderTrackCost:
    @pytest.mark.asyncio
    async def test_track_cost_valid_amount(self):
        provider = ExternalProvider(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost") as mock_add:
            provider._track_cost(0.05, reason="test")
            mock_add.assert_called_once_with(0.05, reason="test")

    @pytest.mark.asyncio
    async def test_track_cost_none(self):
        provider = ExternalProvider(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost") as mock_add:
            provider._track_cost(None, reason="test")
            mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_cost_zero(self):
        provider = ExternalProvider(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost") as mock_add:
            provider._track_cost(0.0, reason="test")
            mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_cost_negative(self):
        provider = ExternalProvider(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost") as mock_add:
            provider._track_cost(-1.0, reason="test")
            mock_add.assert_not_called()


class TestExternalProviderClassVarOverride:
    def test_subclass_overrides_classvars(self):
        class CustomProvider(ExternalProvider):
            default_timeout = 60.0
            default_retries = 5
            retry_delay = 2.0
            retry_on_statuses = frozenset({500, 502, 503})

        provider = CustomProvider(base_url="http://test.local")
        assert provider.default_timeout == 60.0
        assert provider.default_retries == 5
        assert provider.retry_delay == 2.0
        assert 500 in provider.retry_on_statuses


class TestExternalProviderGetJson:
    @pytest.mark.asyncio
    async def test_get_json(self):
        transport = _transport_sequence([_json_response({"data": [1, 2, 3]})])
        provider = _make_provider(transport)
        result = await provider.get_json("/items", params={"page": "1"})
        assert result == {"data": [1, 2, 3]}


# ── StatelessPollingProvider Tests ────────────────────────────


class ConcretePoller(StatelessPollingProvider[str, dict[str, object]]):
    """Test implementation with scripted poll responses."""

    poll_interval = 0.05

    def __init__(self, poll_responses: list[ProviderOutcome[dict[str, object]] | None], **kwargs: object) -> None:
        super().__init__(base_url="http://test.local", **kwargs)
        self._poll_responses = list(poll_responses)
        self._poll_index = 0
        self.submit_calls: list[str] = []
        self.poll_calls: list[str] = []

    async def submit(self, request: str) -> str:
        self.submit_calls.append(request)
        return f"key-{request}"

    async def poll_once(self, key: str) -> ProviderOutcome[dict[str, object]] | None:
        self.poll_calls.append(key)
        idx = min(self._poll_index, len(self._poll_responses) - 1)
        self._poll_index += 1
        return self._poll_responses[idx]


class TestStatelessPollingProviderCall:
    @pytest.mark.asyncio
    async def test_terminal_on_first_poll(self):
        outcome = ProviderOutcome(key="key-req", status="completed", value={"data": 1})
        poller = ConcretePoller(poll_responses=[outcome])

        with patch("ai_pipeline_core.providers.add_cost"):
            result = await poller.call("req", wait=10)

        assert result is not None
        assert result.status == "completed"
        assert result.value == {"data": 1}
        assert len(poller.poll_calls) == 1

    @pytest.mark.asyncio
    async def test_terminal_after_n_polls(self):
        outcome = ProviderOutcome(key="key-req", status="completed", value={"done": True}, cost_usd=0.01)
        poller = ConcretePoller(poll_responses=[None, None, None, outcome])

        with patch("ai_pipeline_core.providers.add_cost") as mock_cost:
            result = await poller.call("req", wait=10)

        assert result is not None
        assert result.status == "completed"
        assert len(poller.poll_calls) == 4
        mock_cost.assert_called_once_with(0.01, reason="ConcretePoller:key-req")

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_status(self):
        poller = ConcretePoller(poll_responses=[None])
        poller.poll_interval = 0.02

        result = await poller.call("req", wait=0.1)

        assert result is not None
        assert result.status == "timeout"
        assert result.error is not None
        assert "timed out" in result.error
        assert len(poller.poll_calls) >= 2

    @pytest.mark.asyncio
    async def test_fire_and_forget(self):
        poller = ConcretePoller(poll_responses=[ProviderOutcome(key="x", status="completed")])

        result = await poller.call("req", wait=0)

        assert result is None
        assert len(poller.submit_calls) == 1
        assert len(poller.poll_calls) == 0

    @pytest.mark.asyncio
    async def test_submit_called_once(self):
        outcome = ProviderOutcome(key="key-req", status="completed", value={})
        poller = ConcretePoller(poll_responses=[None, None, outcome])

        with patch("ai_pipeline_core.providers.add_cost"):
            await poller.call("req", wait=10)

        assert len(poller.submit_calls) == 1

    @pytest.mark.asyncio
    async def test_poll_interval_honored(self):
        outcome = ProviderOutcome(key="key-req", status="completed", value={})
        poller = ConcretePoller(poll_responses=[None, None, outcome])
        poller.poll_interval = 0.1

        sleep_durations: list[float] = []
        original_sleep = asyncio.sleep

        async def recording_sleep(duration: float):
            sleep_durations.append(duration)
            await original_sleep(min(duration, 0.01))

        with patch("ai_pipeline_core.providers.asyncio.sleep", side_effect=recording_sleep), patch("ai_pipeline_core.providers.add_cost"):
            await poller.call("req", wait=10)

        assert len(sleep_durations) == 2
        for d in sleep_durations:
            assert abs(d - 0.1) < 0.05

    @pytest.mark.asyncio
    async def test_custom_terminal_statuses(self):
        class CustomPoller(StatelessPollingProvider[str, str]):
            poll_interval = 0.05
            terminal_statuses = frozenset({"done", "error"})

            async def submit(self, request: str) -> str:
                return "key"

            async def poll_once(self, key: str) -> ProviderOutcome[str] | None:
                return ProviderOutcome(key=key, status="done", value="result")

        poller = CustomPoller(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost"):
            result = await poller.call("req", wait=5)
        assert result is not None
        assert result.status == "done"

    @pytest.mark.asyncio
    async def test_cost_reason_hook(self):
        class LabeledPoller(StatelessPollingProvider[str, dict[str, object]]):
            poll_interval = 0.05

            async def submit(self, request: str) -> str:
                return "key"

            async def poll_once(self, key: str) -> ProviderOutcome[dict[str, object]] | None:
                return ProviderOutcome(key=key, status="completed", cost_usd=0.02)

            def _cost_reason(self, request: str, key: str) -> str:
                return f"custom:{request}"

        poller = LabeledPoller(base_url="http://test.local")
        with patch("ai_pipeline_core.providers.add_cost") as mock_cost:
            await poller.call("my-request", wait=5)
        mock_cost.assert_called_once_with(0.02, reason="custom:my-request")

    @pytest.mark.asyncio
    async def test_negative_wait_is_fire_and_forget(self):
        poller = ConcretePoller(poll_responses=[])
        result = await poller.call("req", wait=-1)
        assert result is None
        assert len(poller.submit_calls) == 1
        assert len(poller.poll_calls) == 0


# ── Top-level re-export tests ────────────────────────────────


class TestReExports:
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

    def test_importable_from_providers_module(self):
        from ai_pipeline_core.providers import ProviderAuthError, ProviderError

        assert ProviderError is not None
        assert ProviderAuthError is not None
