"""Prove ForkGroup, run_chain, and cache TTL warning don't exist.

These flow composition primitives are not yet implemented.
"""

import pytest

from ai_pipeline_core.llm.conversation import Conversation
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class TestForkGroupDoesNotExist:
    """ForkGroup not yet implemented."""

    @pytest.mark.xfail(reason="ForkGroup not yet implemented", strict=True)
    def test_fork_group_importable(self) -> None:
        """After fix: ForkGroup should be importable."""
        from ai_pipeline_core.llm._fork_group import ForkGroup  # type: ignore[import-not-found]

        assert ForkGroup is not None


class TestRunChainDoesNotExist:
    """run_chain not yet implemented."""

    @pytest.mark.xfail(reason="run_chain not yet implemented", strict=True)
    def test_run_chain_importable(self) -> None:
        """After fix: run_chain should be importable."""
        from ai_pipeline_core.llm.conversation import run_chain  # type: ignore[attr-defined]

        assert callable(run_chain)


class TestCacheTTLWarningDoesNotExist:
    """No warmup cache TTL tracking on Conversation."""

    def test_no_last_send_at_field(self) -> None:
        """Passes on current code — no TTL tracking."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert not hasattr(conv, "_last_send_at")

    def test_no_cache_likely_expired_method(self) -> None:
        """Passes on current code — no expiry check."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        assert not hasattr(conv, "cache_likely_expired")
