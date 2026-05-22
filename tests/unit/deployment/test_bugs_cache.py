"""Regression tests for deployment cache key correctness."""

from ai_pipeline_core.deployment._helpers import _build_flow_cache_key


class _FakeFlow:
    __module__ = "my_app.flows"
    __qualname__ = "MyFlow"


def test_flow_cache_key_differs_with_different_params() -> None:
    """Two flows with different constructor params must produce different cache keys.

    Currently _build_flow_cache_key only uses input_fingerprint, module, qualname, and step.
    Changing MyFlow(max_sources=5) vs MyFlow(max_sources=50) produces the same key.
    """
    key1 = _build_flow_cache_key(
        input_fingerprint="abc123",
        flow_class=_FakeFlow,
        step=1,
        flow_params={"max_sources": 5},
    )
    key2 = _build_flow_cache_key(
        input_fingerprint="abc123",
        flow_class=_FakeFlow,
        step=1,
        flow_params={"max_sources": 50},
    )
    assert key1 != key2, "Different flow_params must produce different cache keys"


def test_flow_cache_key_accepts_flow_params() -> None:
    """Verify _build_flow_cache_key accepts flow_params parameter."""
    import inspect

    sig = inspect.signature(_build_flow_cache_key)
    assert "flow_params" in sig.parameters
